"""Backend-owned conversation state machine.

Dolphin proposes (tool calls); the backend decides: tools validate permissions and business state, and the
conversation state only moves through ``apply_tool_result`` after a tool *succeeded*. At the start of each turn
``reconcile`` re-derives the state from PostgreSQL (offers expire, payments arrive, items close while the user
is away), so a stale state never drives the model.

    IDLE ──photo──► PUBLISHING_CONFIRM_IDENTIFICATION ──data──► PUBLISHING_COLLECTING ──complete──►
         PUBLISHING_PREVIEW ──confirm──► IDLE (published)
    IDLE ──search──► BROWSING_RESULTS ──select──► ITEM_SELECTED ──offer──► OFFER_PENDING
    OFFER_PENDING ──owner accepts──► PAYMENT_PENDING ──paid (webhook)──► IDLE (contact released)
    any state with offers to answer as owner ──► OWNER_DECIDING ──accept/reject──► IDLE
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import ConversationSession
from app.models.enums import ItemStatus, OfferStatus, TransactionStatus
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.user import User

log = logging.getLogger(__name__)


class State(StrEnum):
    IDLE = "IDLE"
    PUBLISHING_CONFIRM_IDENTIFICATION = "PUBLISHING_CONFIRM_IDENTIFICATION"
    PUBLISHING_COLLECTING = "PUBLISHING_COLLECTING"
    PUBLISHING_PREVIEW = "PUBLISHING_PREVIEW"
    BROWSING_RESULTS = "BROWSING_RESULTS"
    ITEM_SELECTED = "ITEM_SELECTED"
    OFFER_PENDING = "OFFER_PENDING"
    OWNER_DECIDING = "OWNER_DECIDING"
    PAYMENT_PENDING = "PAYMENT_PENDING"


PUBLISHING = {State.PUBLISHING_CONFIRM_IDENTIFICATION, State.PUBLISHING_COLLECTING, State.PUBLISHING_PREVIEW}

# Users may switch topic at any time, so "entry" states are reachable from everywhere. The remaining edges
# are the only legal ones; anything else is a bug and is refused (state stays unchanged, logged).
_ENTRY = {
    State.IDLE,
    State.PUBLISHING_CONFIRM_IDENTIFICATION,
    State.PUBLISHING_COLLECTING,  # resuming a draft
    State.BROWSING_RESULTS,
    State.ITEM_SELECTED,  # e.g. "me interesa ITEM-XXXXX" seen in the WhatsApp Channel
    State.OWNER_DECIDING,
    State.PAYMENT_PENDING,
    State.OFFER_PENDING,
}
TRANSITIONS: dict[State, set[State]] = {
    State.PUBLISHING_CONFIRM_IDENTIFICATION: {State.PUBLISHING_COLLECTING, State.PUBLISHING_PREVIEW},
    State.PUBLISHING_COLLECTING: {State.PUBLISHING_COLLECTING, State.PUBLISHING_PREVIEW},
    State.PUBLISHING_PREVIEW: {State.PUBLISHING_COLLECTING, State.PUBLISHING_PREVIEW},
    State.BROWSING_RESULTS: {State.ITEM_SELECTED},
    State.ITEM_SELECTED: {State.ITEM_SELECTED},
    State.OFFER_PENDING: set(),
    State.OWNER_DECIDING: set(),
    State.PAYMENT_PENDING: set(),
    State.IDLE: set(),
}


def can_transition(src: str, dst: State) -> bool:
    try:
        s = State(src)
    except ValueError:
        return True  # unknown legacy value: always allow moving to a known state
    return dst in _ENTRY or dst in TRANSITIONS.get(s, set())


def transition(conv: ConversationSession, dst: State, *, reason: str) -> bool:
    if conv.conversation_state == dst:
        return True
    if not can_transition(conv.conversation_state, dst):
        log.warning("refused state transition %s -> %s (%s)", conv.conversation_state, dst, reason)
        return False
    conv.conversation_state = dst.value
    return True


def apply_tool_result(conv: ConversationSession, tool: str, result: dict[str, Any]) -> None:
    """Move the state after a SUCCESSFUL tool call (the only way tools influence the state)."""
    if not result.get("ok"):
        return
    if tool == "create_publication":
        transition(conv, State.PUBLISHING_CONFIRM_IDENTIFICATION, reason=tool)
    elif tool == "update_publication":
        if result.get("status") == "PUBLISHED":
            transition(conv, State.IDLE, reason="published")
        elif result.get("status") == "DRAFT":
            transition(conv, State.PUBLISHING_COLLECTING, reason=tool)
    elif tool == "get_publication" and result.get("preview"):
        transition(conv, State.PUBLISHING_PREVIEW, reason="preview_shown")
    elif tool in ("search_publications", "get_search_page"):
        transition(conv, State.BROWSING_RESULTS, reason=tool)
    elif tool == "select_publication":
        transition(conv, State.ITEM_SELECTED, reason=tool)
    elif tool == "create_offer":
        transition(conv, State.OFFER_PENDING, reason=tool)
    elif tool in ("accept_offer", "reject_offer", "cancel_offer", "delete_publication"):
        transition(conv, State.IDLE, reason=tool)
    elif tool == "create_payment":
        transition(conv, State.PAYMENT_PENDING, reason=tool)


def reconcile(session: Session, user: User, conv: ConversationSession) -> State:
    """Re-derive the state from the database before each turn."""
    state = conv.conversation_state
    if state in PUBLISHING:
        item = session.get(Item, conv.current_item_id) if conv.current_item_id else None
        if item is None or item.status != ItemStatus.DRAFT:
            transition(conv, State.IDLE, reason="draft_gone")
    if state == State.ITEM_SELECTED:
        item = session.get(Item, conv.selected_item_id) if conv.selected_item_id else None
        if item is None or item.status != ItemStatus.PUBLISHED:
            transition(conv, State.BROWSING_RESULTS if conv.search_session_id else State.IDLE, reason="item_gone")
    if state == State.OFFER_PENDING:
        offer = session.get(Offer, conv.current_offer_id) if conv.current_offer_id else None
        if offer is None or offer.status != OfferStatus.PENDING:
            if offer is not None and offer.status == OfferStatus.PAYMENT_PENDING:
                tx = session.execute(select(Transaction).where(Transaction.offer_id == offer.id)).scalar_one_or_none()
                conv.transaction_id = tx.id if tx else conv.transaction_id
                transition(conv, State.PAYMENT_PENDING, reason="offer_accepted")
            else:
                transition(conv, State.IDLE, reason="offer_closed")
    if conv.conversation_state == State.PAYMENT_PENDING:
        tx = session.get(Transaction, conv.transaction_id) if conv.transaction_id else None
        if tx is None:
            tx = (
                session.execute(
                    select(Transaction).where(
                        Transaction.bidder_id == user.id, Transaction.status == TransactionStatus.PAYMENT_PENDING
                    )
                )
                .scalars()
                .first()
            )
            conv.transaction_id = tx.id if tx else None
        if tx is None or tx.status != TransactionStatus.PAYMENT_PENDING:
            transition(conv, State.IDLE, reason="payment_window_closed")
    if conv.conversation_state == State.IDLE:
        accepted = session.execute(
            select(Transaction.id).where(
                Transaction.bidder_id == user.id, Transaction.status == TransactionStatus.PAYMENT_PENDING
            )
        ).first()
        if accepted is not None:
            conv.transaction_id = accepted[0]
            transition(conv, State.PAYMENT_PENDING, reason="accepted_offer_awaiting_payment")
        elif session.execute(
            select(Offer.id).where(Offer.owner_id == user.id, Offer.status == OfferStatus.PENDING)
        ).first():
            transition(conv, State.OWNER_DECIDING, reason="offers_to_answer")
    if (
        conv.conversation_state == State.OWNER_DECIDING
        and not session.execute(
            select(Offer.id).where(Offer.owner_id == user.id, Offer.status == OfferStatus.PENDING)
        ).first()
    ):
        transition(conv, State.IDLE, reason="no_offers_to_answer")
    return State(conv.conversation_state) if conv.conversation_state in State.__members__ else State.IDLE


_EXPECTED = {
    State.IDLE: "Entender qué quiere la persona: publicar (pedir foto), buscar, o consultar sus operaciones.",
    State.PUBLISHING_CONFIRM_IDENTIFICATION: "Pedir que confirme qué artículo es (identificación sugerida).",
    State.PUBLISHING_COLLECTING: "Pedir SOLO los datos que faltan (ver missing_fields) y guardarlos con "
    "update_publication. Cuando no falte nada, mostrar la vista previa con get_publication.",
    State.PUBLISHING_PREVIEW: "La vista previa ya se mostró: si confirma, publicar con update_publication "
    "(publish=true); si quiere cambiar algo, actualizarlo.",
    State.BROWSING_RESULTS: "Ayudar a elegir un resultado (número, código o descripción) o mostrar más.",
    State.ITEM_SELECTED: "Mostrar el artículo elegido y, si quiere, crear la oferta (dinero, permuta o combinada). "
    "Recordar que la oferta vence en 24h.",
    State.OFFER_PENDING: "La oferta espera respuesta del propietario (vence en 24h). Puede cancelarla.",
    State.OWNER_DECIDING: "Tiene ofertas por responder (pending_offers_to_answer): aceptar o rechazar antes de "
    "que caduquen (24h).",
    State.PAYMENT_PENDING: "Le aceptaron la oferta: ofrecer el link de pago (create_payment). Solo el backend "
    "confirma pagos (get_payment_status).",
}


def expected_next(state: State) -> str:
    return _EXPECTED.get(state, _EXPECTED[State.IDLE])
