"""Allowlisted Dolphin tools (section 36). The ONLY bridge between the model and the backend.

* Exactly the 20 tools from the spec; anything else is denied and recorded as a security event.
* Arguments are validated with strict Pydantic models (``extra="forbid"``): the model can never pass
  user_id/owner_id/status/amount — identity always comes from the backend session.
* Every tool calls the same deterministic services used everywhere else (ownership, state machine,
  system mode, anti-evasion, payments verification).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError, NotFound, ValidationFailed
from app.core.text import clean_user_text
from app.models.conversation import ConversationSession
from app.models.enums import ActorType, ItemStatus, ReportReason, SecurityEventType, Severity
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.report import Report
from app.models.user import User
from app.services.audit.service import AuditAction, audit
from app.services.conversation import state_machine
from app.services.mercadopago.gateway import PaymentGateway
from app.services.offers import service as offers
from app.services.payments import service as payments
from app.services.publications import service as pubs
from app.services.publications.image_validation import ImageRejected
from app.services.publications.storage import ImageStorage
from app.services.search import service as search
from app.services.security.anti_evasion import detect
from app.services.security.enforcement import EvasionBlocked, record_security_event

log = logging.getLogger(__name__)

ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "create_publication",
        "get_my_publications",
        "get_publication",
        "update_publication",
        "delete_publication",
        "search_publications",
        "get_search_page",
        "select_publication",
        "create_offer",
        "get_offer",
        "cancel_offer",
        "accept_offer",
        "reject_offer",
        "get_transaction",
        "create_payment",
        "get_payment_status",
        "send_message",
        "warn_user",
        "report_publication",
        "get_user_context",
    }
)
FORBIDDEN_TOOLS: frozenset[str] = frozenset({"execute_sql", "execute_shell", "execute_code", "filesystem_access"})

ItemCode = Field(pattern=r"^ITEM-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5,8}$")
OfferCode = Field(pattern=r"^OFR-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5,8}$")
TxCode = Field(pattern=r"^OP-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5,8}$")


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NoArgs(_Args):
    pass


class CreatePublicationArgs(_Args):
    """Create a DRAFT from the photo the user just sent, with the vision suggestion (never a price)."""

    title: str | None = Field(None, max_length=120)
    category: str | None = Field(None, max_length=60)
    brand: str | None = Field(None, max_length=60)
    model: str | None = Field(None, max_length=60)
    confidence: float | None = Field(None, ge=0, le=1)


class GetPublicationArgs(_Args):
    item_id: str = ItemCode


class UpdatePublicationArgs(_Args):
    item_id: str = ItemCode
    title: str | None = Field(None, max_length=120)
    category: str | None = Field(None, max_length=60)
    identification_confirmed: bool | None = None
    zone: str | None = Field(None, max_length=80)
    price: str | None = Field(None, max_length=20, description="Exact price stated by the user")
    accepts_swap: bool | None = None
    has_damage: bool | None = None
    damage_description: str | None = Field(None, max_length=500)
    works: bool | None = None
    malfunction_description: str | None = Field(None, max_length=500)
    description: str | None = Field(None, max_length=1000)
    publish: bool = Field(False, description="Publish a DRAFT. Only after the user confirmed the preview.")


class DeletePublicationArgs(_Args):
    reference: str = Field(..., max_length=120, description="ITEM code or how the user named it")


class SearchArgs(_Args):
    query: str = Field(..., min_length=1, max_length=200)
    zone: str | None = Field(None, max_length=80)
    max_price: str | None = Field(None, max_length=20)
    accepts_swap: bool | None = None
    category: str | None = Field(None, max_length=60)


class SelectArgs(_Args):
    reference: str = Field(..., max_length=120, description="Number 1-5, ordinal, ITEM code or description")


class CreateOfferArgs(_Args):
    item_id: str | None = Field(None, pattern=r"^ITEM-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5,8}$")
    offer_type: Literal["MONEY", "SWAP", "COMBINED"]
    offered_price: str | None = Field(None, max_length=20)
    offered_item: str | None = Field(None, max_length=300)
    message: str | None = Field(None, max_length=500)


class OfferRefArgs(_Args):
    offer_id: str | None = Field(None, pattern=r"^OFR-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5,8}$")


class TxRefArgs(_Args):
    transaction_id: str | None = Field(None, pattern=r"^OP-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5,8}$")


class SendMessageArgs(_Args):
    text: str = Field(..., min_length=1, max_length=1500)


class WarnUserArgs(_Args):
    reason: str = Field(..., max_length=300)


class ReportArgs(_Args):
    item_id: str = ItemCode
    reason: Literal["FRAUD", "FAKE_ITEM", "PROHIBITED_ITEM", "SPAM", "WRONG_INFO", "SUSPICIOUS_BEHAVIOR"]
    details: str | None = Field(None, max_length=500)


@dataclass
class ToolContext:
    session: Session
    user: User
    conversation: ConversationSession
    gateway: PaymentGateway | None = None
    storage: ImageStorage | None = None
    inbound_images: list[bytes] = field(default_factory=list)
    outbox: list[str] = field(default_factory=list)  # messages queued via send_message (model text: filtered)
    # Messages authored by the BACKEND (e.g. the official Mercado Pago link). Delivered verbatim, never passed
    # through the model or the output filter (which would block the link as an "external URL").
    backend_messages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[_Args]
    handler: Callable[[ToolContext, Any], dict[str, Any]]


def _item_brief(session: Session, item: Item, *, owner_view: bool) -> dict[str, Any]:
    d = pubs.public_view(session, item)
    if owner_view:
        d["status"] = item.status.value
        d["expires_at"] = item.expires_at.isoformat() if item.expires_at else None
    return d


# ---- handlers --------------------------------------------------------------------------------
def _create_publication(ctx: ToolContext, a: CreatePublicationArgs) -> dict[str, Any]:
    item = pubs.start_draft(ctx.session, ctx.user, ctx.inbound_images, storage=ctx.storage)
    pubs.set_ai_identification(ctx.session, ctx.user, item.id, a.model_dump(exclude_none=True))
    ctx.conversation.current_item_id = item.id
    return {
        "ok": True,
        "item_id": item.public_id,
        "status": item.status.value,
        "suggestion": item.ai_identification,
        "missing": pubs.missing_fields(item),
        "next": "Confirmá con el usuario qué artículo es y pedí los datos básicos.",
    }


def _get_my_publications(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    items = pubs.list_my_publications(ctx.session, ctx.user)
    return {"ok": True, "items": [_item_brief(ctx.session, i, owner_view=True) for i in items]}


def _get_publication(ctx: ToolContext, a: GetPublicationArgs) -> dict[str, Any]:
    item = pubs.get_by_public_id(ctx.session, a.item_id)
    if item.owner_id == ctx.user.id:
        out = {"ok": True, "owner": True, **_item_brief(ctx.session, item, owner_view=True)}
        if item.status == ItemStatus.DRAFT:
            out["missing"] = pubs.missing_fields(item)
            if not out["missing"]:
                out["preview"] = pubs.preview_text(ctx.session, item)
                ctx.conversation.context = {**ctx.conversation.context, "preview_shown_for": item.public_id}
        return out
    if item.status not in (ItemStatus.PUBLISHED,):
        return {"ok": True, "item_id": item.public_id, "available": False}
    item.view_count += 1
    return {"ok": True, "owner": False, **_item_brief(ctx.session, item, owner_view=False)}


def _update_publication(ctx: ToolContext, a: UpdatePublicationArgs) -> dict[str, Any]:
    item = pubs.get_by_public_id(ctx.session, a.item_id)
    if item.owner_id != ctx.user.id:
        raise DomainError("Solo el propietario puede modificar la publicación.", code="NOT_OWNER")
    if item.status == ItemStatus.DRAFT:
        pubs.fill_draft(
            ctx.session,
            ctx.user,
            item.id,
            title=a.title,
            category=a.category,
            identification_confirmed=a.identification_confirmed,
            zone=a.zone,
            base_price=a.price,
            accepts_swap=a.accepts_swap,
            has_damage=a.has_damage,
            damage_description=a.damage_description,
            works=a.works,
            malfunction_description=a.malfunction_description,
            description=a.description,
        )
        # Any change after the preview invalidates it.
        changed = any(v is not None for k, v in a.model_dump().items() if k not in ("item_id", "publish"))
        if changed and ctx.conversation.context.get("preview_shown_for") == item.public_id:
            ctx.conversation.context = {k: v for k, v in ctx.conversation.context.items() if k != "preview_shown_for"}
        if a.publish:
            if ctx.conversation.context.get("preview_shown_for") != item.public_id:
                return {
                    "ok": False,
                    "error": "PREVIEW_NOT_CONFIRMED",
                    "message": "Primero mostrale la previsualización (get_publication) y pedile confirmación.",
                }
            pubs.publish(ctx.session, ctx.user, item.id)
            ctx.conversation.context = {k: v for k, v in ctx.conversation.context.items() if k != "preview_shown_for"}
            from app.services.notifications import templates
            from app.services.system import config_service as cfg

            return {
                "ok": True,
                "item_id": item.public_id,
                "status": "PUBLISHED",
                "message": templates.publication_published(
                    item_code=item.public_id, title=item.title or "", days=cfg.publication_days(ctx.session)
                ),
            }
        return {
            "ok": True,
            "item_id": item.public_id,
            "status": item.status.value,
            "missing": pubs.missing_fields(item),
        }
    changes: dict[str, Any] = {}
    for src, dst in (
        ("price", "base_price"),
        ("zone", "zone"),
        ("accepts_swap", "accepts_swap"),
        ("has_damage", "has_damage"),
        ("damage_description", "damage_description"),
        ("works", "works"),
        ("malfunction_description", "malfunction_description"),
        ("description", "description"),
        ("title", "title"),
        ("category", "category"),
    ):
        v = getattr(a, src)
        if v is not None:
            changes[dst] = v
    pubs.update_publication(ctx.session, ctx.user, item.id, changes)
    return {"ok": True, "item_id": item.public_id, "updated": sorted(changes)}


def _delete_publication(ctx: ToolContext, a: DeletePublicationArgs) -> dict[str, Any]:
    item = pubs.resolve_owned_item(ctx.session, ctx.user, a.reference)
    pubs.delete_publication(ctx.session, ctx.user, item.id)
    return {"ok": True, "item_id": item.public_id, "status": "DELETED"}


def _search_result(ctx: ToolContext, page: search.SearchPage) -> dict[str, Any]:
    ctx.conversation.search_session_id = page.search_session_id
    return {
        "ok": True,
        "page": page.page,
        "results": page.results,
        "exhausted": page.exhausted,
        "hint": "Mostrá los resultados numerados. Para ver más: 'más', 'siguiente' o 'ver más'.",
    }


def _search_publications(ctx: ToolContext, a: SearchArgs) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if a.max_price:
        overrides["max_price"] = str(pubs.parse_price(a.max_price))
    if a.accepts_swap is not None:
        overrides["accepts_swap"] = a.accepts_swap
    if a.category:
        overrides["category"] = a.category
    if a.zone:
        try:
            overrides["zone_slug"] = pubs.resolve_zone(ctx.session, a.zone).slug
        except DomainError:
            pass
    page = search.start_search(ctx.session, ctx.user, a.query, overrides=overrides)
    return _search_result(ctx, page)


def _get_search_page(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    if not ctx.conversation.search_session_id:
        raise NotFound("No hay una búsqueda activa.", code="SEARCH_NOT_FOUND")
    page = search.next_page(ctx.session, ctx.user, ctx.conversation.search_session_id)
    return _search_result(ctx, page)


def _select_publication(ctx: ToolContext, a: SelectArgs) -> dict[str, Any]:
    item = search.resolve_selection(
        ctx.session, ctx.user, a.reference, search_session_id=ctx.conversation.search_session_id
    )
    ctx.conversation.selected_item_id = item.id
    return {"ok": True, **_item_brief(ctx.session, item, owner_view=False)}


def _create_offer(ctx: ToolContext, a: CreateOfferArgs) -> dict[str, Any]:
    code = a.item_id
    if code is None:
        if not ctx.conversation.selected_item_id:
            raise ValidationFailed("Primero elegí un artículo.", code="NO_ITEM_SELECTED")
        sel = ctx.session.get(Item, ctx.conversation.selected_item_id)
        code = sel.public_id if sel else None
    res = offers.create_offer(
        ctx.session,
        ctx.user,
        code or "",
        offer_type=a.offer_type,
        offered_price=a.offered_price,
        offered_item=a.offered_item,
        message=a.message,
    )
    ctx.conversation.current_offer_id = res.offer.id
    return {
        "ok": True,
        "offer_id": res.offer.public_id,
        "status": res.offer.status.value,
        "expires_at": res.offer.expires_at.isoformat(),
        "message_for_user": res.bidder_message,
    }


def _resolve_offer_code(ctx: ToolContext, code: str | None) -> str:
    if code:
        return code
    if ctx.conversation.current_offer_id:
        o = ctx.session.get(Offer, ctx.conversation.current_offer_id)
        if o:
            return o.public_id
    # Owner answering: the single pending offer addressed to them, if exactly one.
    pending = (
        ctx.session.execute(select(Offer).where(Offer.owner_id == ctx.user.id, Offer.status.in_(offers.UNPAID_ACTIVE)))
        .scalars()
        .all()
    )
    if len(pending) == 1:
        return pending[0].public_id
    if len(pending) > 1:
        from app.core.errors import Ambiguous

        raise Ambiguous("¿Sobre qué oferta?", [{"offer_id": o.public_id, "status": o.status.value} for o in pending])
    raise NotFound("No encontré una oferta activa.", code="OFFER_NOT_FOUND")


def _get_offer(ctx: ToolContext, a: OfferRefArgs) -> dict[str, Any]:
    offer = offers.get_offer_for_user(ctx.session, ctx.user, _resolve_offer_code(ctx, a.offer_id))
    return {"ok": True, **offers.offer_view(ctx.session, offer, ctx.user)}


def _cancel_offer(ctx: ToolContext, a: OfferRefArgs) -> dict[str, Any]:
    code = _resolve_offer_code(ctx, a.offer_id)
    offer = offers.get_offer_for_user(ctx.session, ctx.user, code)
    if offer.bidder_id == ctx.user.id:
        res = offers.cancel_offer(ctx.session, ctx.user, code)
        return {"ok": True, "offer_id": code, "status": res.offer.status.value, "message_for_user": res.bidder_message}
    # Owner backing out after acceptance: first ask the provider whether the bidder already paid.
    tx = offers.get_transaction_for_offer(ctx.session, offer.id)
    if tx is not None and ctx.gateway is not None:
        payments.sync_transaction_payments(ctx.session, tx, ctx.gateway)
    res = offers.owner_withdraw(ctx.session, ctx.user, code)
    return {"ok": True, "offer_id": code, "status": res.offer.status.value, "message_for_user": res.owner_message}


def _accept_offer(ctx: ToolContext, a: OfferRefArgs) -> dict[str, Any]:
    res = offers.accept_offer(ctx.session, ctx.user, _resolve_offer_code(ctx, a.offer_id))
    assert res.transaction is not None
    return {
        "ok": True,
        "offer_id": res.offer.public_id,
        "status": res.offer.status.value,
        "transaction_id": res.transaction.public_id,
        "message_for_user": res.owner_message,
    }


def _reject_offer(ctx: ToolContext, a: OfferRefArgs) -> dict[str, Any]:
    res = offers.reject_offer(ctx.session, ctx.user, _resolve_offer_code(ctx, a.offer_id))
    return {
        "ok": True,
        "offer_id": res.offer.public_id,
        "status": res.offer.status.value,
        "message_for_user": res.owner_message,
    }


def _resolve_tx_code(ctx: ToolContext, code: str | None) -> str:
    if code:
        return code
    if ctx.conversation.transaction_id:
        tx = ctx.session.get(Transaction, ctx.conversation.transaction_id)
        if tx:
            return tx.public_id
    from app.models.enums import TransactionStatus

    txs = (
        ctx.session.execute(
            select(Transaction).where(
                Transaction.bidder_id == ctx.user.id, Transaction.status == TransactionStatus.PAYMENT_PENDING
            )
        )
        .scalars()
        .all()
    )
    if len(txs) == 1:
        return txs[0].public_id
    raise NotFound("No encontré una operación pendiente de pago.", code="TRANSACTION_NOT_FOUND")


def _get_transaction(ctx: ToolContext, a: TxRefArgs) -> dict[str, Any]:
    return {"ok": True, **payments.transaction_view(ctx.session, ctx.user, _resolve_tx_code(ctx, a.transaction_id))}


def _create_payment(ctx: ToolContext, a: TxRefArgs) -> dict[str, Any]:
    if ctx.gateway is None:
        raise DomainError("Los pagos no están disponibles en este momento.", code="PAYMENTS_UNAVAILABLE")
    link = payments.create_payment(ctx.session, ctx.user, _resolve_tx_code(ctx, a.transaction_id), ctx.gateway)
    ctx.conversation.transaction_id = link.payment.transaction_id
    if link.message not in ctx.backend_messages:
        ctx.backend_messages.append(link.message)
    return {
        "ok": True,
        "checkout_url": link.url,
        "link_already_sent_by_backend": True,
        "note": "El backend ya le envió el link oficial de Mercado Pago. No repitas el link.",
    }


def _get_payment_status(ctx: ToolContext, a: TxRefArgs) -> dict[str, Any]:
    status = payments.get_payment_status(ctx.session, ctx.user, _resolve_tx_code(ctx, a.transaction_id), ctx.gateway)
    status["note"] = "Estado verificado por el backend con Mercado Pago. La palabra del usuario no cuenta."
    return {"ok": True, **status}


def _send_message(ctx: ToolContext, a: SendMessageArgs) -> dict[str, Any]:
    ctx.outbox.append(a.text)  # recipient is always the current user; output filter applied later
    return {"ok": True, "queued": True}


def _warn_user(ctx: ToolContext, a: WarnUserArgs) -> dict[str, Any]:
    """Dolphin may only FLAG a suspicion for admins. It cannot add strikes, warn formally or ban:
    the deterministic detector + backend own the counter (section 38)."""
    record_security_event(
        ctx.session,
        user_id=ctx.user.id,
        event_type=SecurityEventType.AI_FLAGGED_SUSPICION,
        severity=Severity.LOW,
        details={"reason": clean_user_text(a.reason, 300)},
    )
    return {"ok": True, "flagged_for_review": True, "strike_added": False}


def _report_publication(ctx: ToolContext, a: ReportArgs) -> dict[str, Any]:
    from sqlalchemy.exc import IntegrityError

    item = pubs.get_by_public_id(ctx.session, a.item_id)
    report = Report(
        reporter_id=ctx.user.id,
        item_id=item.id,
        reported_user_id=item.owner_id,
        reason=ReportReason(a.reason),
        details=clean_user_text(a.details, 500),
    )
    try:
        with ctx.session.begin_nested():
            ctx.session.add(report)
            ctx.session.flush()
    except IntegrityError:
        return {"ok": True, "already_reported": True}
    audit(
        ctx.session,
        AuditAction.REPORT_CREATED,
        actor_type=ActorType.USER,
        actor_id=ctx.user.id,
        entity_type="report",
        entity_id=report.id,
        details={"item": item.public_id, "reason": a.reason},
    )
    return {"ok": True, "report_id": report.id}


def _get_user_context(ctx: ToolContext, a: NoArgs) -> dict[str, Any]:
    from app.services.dolphin.context import build_context

    return {"ok": True, **build_context(ctx.session, ctx.user, ctx.conversation)}


TOOLS: dict[str, ToolSpec] = {
    s.name: s
    for s in [
        ToolSpec(
            "create_publication",
            "Crear un BORRADOR con la foto que el usuario acaba de enviar y tu sugerencia "
            "de identificación (nunca precio).",
            CreatePublicationArgs,
            _create_publication,
        ),
        ToolSpec("get_my_publications", "Listar las publicaciones activas del usuario.", NoArgs, _get_my_publications),
        ToolSpec(
            "get_publication",
            "Ver una publicación por código. Si es un borrador completo del usuario devuelve la previsualización.",
            GetPublicationArgs,
            _get_publication,
        ),
        ToolSpec(
            "update_publication",
            "Completar un borrador (datos básicos, publish=true tras confirmar la "
            "previsualización) o editar una publicación propia.",
            UpdatePublicationArgs,
            _update_publication,
        ),
        ToolSpec(
            "delete_publication",
            "Borrar una publicación propia (por código o como la nombró el usuario).",
            DeletePublicationArgs,
            _delete_publication,
        ),
        ToolSpec(
            "search_publications",
            "Buscar artículos en lenguaje natural. Devuelve hasta 5 resultados.",
            SearchArgs,
            _search_publications,
        ),
        ToolSpec(
            "get_search_page", "Siguiente página (5 más, sin repetir) de la búsqueda actual.", NoArgs, _get_search_page
        ),
        ToolSpec(
            "select_publication",
            "Elegir un artículo por número, código o descripción.",
            SelectArgs,
            _select_publication,
        ),
        ToolSpec(
            "create_offer",
            "Hacer una oferta (MONEY, SWAP o COMBINED) por el artículo elegido. Vence en 24h.",
            CreateOfferArgs,
            _create_offer,
        ),
        ToolSpec("get_offer", "Ver el estado de una oferta.", OfferRefArgs, _get_offer),
        ToolSpec(
            "cancel_offer",
            "Ofertante: cancelar su oferta. Propietario: arrepentirse de una oferta aceptada "
            "mientras no se haya pagado.",
            OfferRefArgs,
            _cancel_offer,
        ),
        ToolSpec("accept_offer", "Propietario: aceptar una oferta pendiente.", OfferRefArgs, _accept_offer),
        ToolSpec("reject_offer", "Propietario: rechazar una oferta pendiente.", OfferRefArgs, _reject_offer),
        ToolSpec("get_transaction", "Ver una operación.", TxRefArgs, _get_transaction),
        ToolSpec(
            "create_payment",
            "Ofertante: generar el link oficial de Mercado Pago. El backend se lo envía directamente; no lo repitas.",
            TxRefArgs,
            _create_payment,
        ),
        ToolSpec(
            "get_payment_status",
            "Consultar el estado VERIFICADO del pago (consulta a Mercado Pago).",
            TxRefArgs,
            _get_payment_status,
        ),
        ToolSpec("send_message", "Enviar un mensaje adicional al usuario actual.", SendMessageArgs, _send_message),
        ToolSpec(
            "warn_user",
            "Marcar una sospecha para revisión de un administrador (no suma advertencias).",
            WarnUserArgs,
            _warn_user,
        ),
        ToolSpec("report_publication", "Reportar una publicación.", ReportArgs, _report_publication),
        ToolSpec("get_user_context", "Contexto verificado de la conversación.", NoArgs, _get_user_context),
    ]
}

assert set(TOOLS) == ALLOWED_TOOLS, "tool registry must match the allowlist exactly"
assert not (set(TOOLS) & FORBIDDEN_TOOLS)


def openai_tools() -> list[dict[str, Any]]:
    out = []
    for spec in TOOLS.values():
        schema = spec.args.model_json_schema()
        schema.pop("title", None)
        schema.pop("description", None)
        out.append(
            {"type": "function", "function": {"name": spec.name, "description": spec.description, "parameters": schema}}
        )
    return out


def execute(ctx: ToolContext, name: str, raw_arguments: str | dict[str, Any] | None) -> dict[str, Any]:
    """Validate and run a tool call proposed by Dolphin. Never raises: returns a JSON-able result."""
    if name not in ALLOWED_TOOLS or name not in TOOLS:
        record_security_event(
            ctx.session,
            user_id=ctx.user.id,
            event_type=SecurityEventType.TOOL_DENIED,
            severity=Severity.HIGH if name in FORBIDDEN_TOOLS else Severity.MEDIUM,
            details={"tool": str(name)[:80]},
        )
        audit(
            ctx.session,
            AuditAction.DOLPHIN_TOOL_DENIED,
            actor_type=ActorType.SYSTEM,
            entity_type="user",
            entity_id=ctx.user.id,
            details={"tool": str(name)[:80]},
        )
        return {"ok": False, "error": "TOOL_NOT_ALLOWED", "message": "Esa herramienta no existe."}
    spec = TOOLS[name]
    try:
        data = json.loads(raw_arguments) if isinstance(raw_arguments, str) else (raw_arguments or {})
        if not isinstance(data, dict):
            raise ValueError("arguments must be an object")
        args = spec.args.model_validate(data)
    except (ValueError, ValidationError) as e:
        errs = e.errors(include_url=False, include_input=False) if isinstance(e, ValidationError) else str(e)
        return {"ok": False, "error": "INVALID_ARGUMENTS", "details": json.loads(json.dumps(errs, default=str))}
    sp = ctx.session.begin_nested()
    try:
        result = spec.handler(ctx, args)
        sp.commit()
        state_machine.apply_tool_result(ctx.conversation, name, result)
        return result
    except EvasionBlocked as e:
        # Strike already committed by enforce_or_raise (the savepoint is gone with that commit).
        return {"ok": False, "error": e.code, "message": e.message, **e.details}
    except ImageRejected as e:
        sp.rollback()
        return {"ok": False, "error": e.code, "message": e.message, **e.details}
    except DomainError as e:
        sp.rollback()
        return {"ok": False, **e.to_dict()}


def output_filter(text: str) -> str:
    """Last line of defence: model output never carries contact data."""
    if detect(text).detected:
        return "Perdón, no puedo compartir ese tipo de información. ¿Seguimos con tu operación?"
    return text


__all__ = ["ALLOWED_TOOLS", "FORBIDDEN_TOOLS", "TOOLS", "ToolContext", "execute", "openai_tools", "output_filter"]
