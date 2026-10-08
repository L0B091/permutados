"""Builds the minimal, secret-free context Dolphin receives (section 37/40).

Allowlisted fields only. Public codes instead of internal ids. Never phones, WhatsApp ids, names of other
users, tokens, credentials or configuration secrets.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.conversation import ConversationSession, SearchSession
from app.models.enums import OfferStatus
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.user import User
from app.services.notifications.templates import money
from app.services.system import config_service as cfg

CONTEXT_KEYS = frozenset(
    {
        "session_id",
        "conversation_state",
        "current_intent",
        "current_item_id",
        "search_session_id",
        "selected_item_id",
        "current_offer_id",
        "transaction_id",
        "user_status",
        "evasion_strikes",
        "pending_offers_to_answer",
        "my_active_offers",
        "contact_release_fee",
        "offer_expiration_hours",
        "payment_window_hours",
        "system_mode",
        "last_results",
    }
)


def _code(session: Session, model: type, pk: int | None) -> str | None:
    if not pk:
        return None
    obj = session.get(model, pk)
    return getattr(obj, "public_id", None) if obj else None


def build_context(session: Session, user: User, conv: ConversationSession) -> dict[str, Any]:
    amount, currency = cfg.release_price(session)
    pending_for_owner = (
        session.execute(select(Offer).where(Offer.owner_id == user.id, Offer.status == OfferStatus.PENDING))
        .scalars()
        .all()
    )
    mine = (
        session.execute(
            select(Offer).where(
                Offer.bidder_id == user.id,
                Offer.status.in_([OfferStatus.PENDING, OfferStatus.PAYMENT_PENDING, OfferStatus.PAID]),
            )
        )
        .scalars()
        .all()
    )
    last_results: list[dict[str, Any]] = []
    if conv.search_session_id:
        ss = session.get(SearchSession, conv.search_session_id)
        if ss:
            for n, iid in enumerate(ss.last_page_item_ids, start=1):
                it = session.get(Item, iid)
                if it:
                    last_results.append({"n": n, "item_id": it.public_id, "title": it.title})
    ctx = {
        "session_id": conv.id,
        "conversation_state": conv.conversation_state,
        "current_intent": conv.current_intent,
        "current_item_id": _code(session, Item, conv.current_item_id),
        "search_session_id": conv.search_session_id,
        "selected_item_id": _code(session, Item, conv.selected_item_id),
        "current_offer_id": _code(session, Offer, conv.current_offer_id),
        "transaction_id": _code(session, Transaction, conv.transaction_id),
        "user_status": user.status.value,
        "evasion_strikes": user.evasion_strikes,
        "pending_offers_to_answer": [
            {"offer_id": o.public_id, "expires_at": o.expires_at.isoformat()} for o in pending_for_owner
        ],
        "my_active_offers": [{"offer_id": o.public_id, "status": o.status.value} for o in mine],
        "contact_release_fee": money(amount, currency),
        "offer_expiration_hours": cfg.offer_hours(session),
        "payment_window_hours": cfg.payment_hours(session),
        "system_mode": cfg.current_mode(session).value,
        "last_results": last_results,
    }
    assert set(ctx) <= CONTEXT_KEYS
    return ctx
