"""Admin endpoints added for the Android console (phase 4): detail views, moderation edits, conversation
browser, warnings and audit log. Same rules as ``routes.py``: RBAC, masked PII, every sensitive view audited."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import AdminPrincipal, get_db, require_role
from app.api.routes.admin.routes import _item_out, _page, _user_out
from app.core.errors import InvalidState, NotFound, ValidationFailed
from app.models.audit import AuditLog
from app.models.channel import ChannelPost
from app.models.conversation import ConversationSession, Message, Notification
from app.models.enums import ActorType, AdminRole, ItemStatus
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.payment import Payment
from app.models.report import Report
from app.models.security import Warning
from app.models.user import User, Zone
from app.services.audit.service import AuditAction, audit
from app.services.publications import service as pubs
from app.services.security.anti_evasion import detect
from app.services.users import pii

router = APIRouter(prefix="/admin", tags=["admin-console"])
VIEWER = require_role(AdminRole.VIEWER)
MODERATOR = require_role(AdminRole.MODERATOR)
ADMIN = require_role(AdminRole.ADMIN)

CLOSED = (ItemStatus.COMPLETED, ItemStatus.EXPIRED, ItemStatus.DELETED)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _codes(db: Session, model, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return dict(db.execute(select(model.id, model.public_id).where(model.id.in_(ids))).all())


def offer_out(o: Offer, item_codes: dict[int, str], titles: dict[int, str | None] | None = None) -> dict[str, Any]:
    return {
        "offer_id": o.public_id,
        "item_id": o.item_id,
        "item_code": item_codes.get(o.item_id),
        "item_title": (titles or {}).get(o.item_id),
        "bidder_id": o.bidder_id,
        "owner_id": o.owner_id,
        "type": o.offer_type.value,
        "price": str(o.offered_price) if o.offered_price is not None else None,
        "offered_item": o.offered_item,
        "status": o.status.value,
        "close_reason": o.close_reason.value if o.close_reason else None,
        "created_at": _iso(o.created_at),
        "expires_at": _iso(o.expires_at),
        "responded_at": _iso(o.responded_at),
        "closed_at": _iso(o.closed_at),
    }


def _report_out(r: Report) -> dict[str, Any]:
    return {
        "id": r.id,
        "reporter_id": r.reporter_id,
        "item_id": r.item_id,
        "reported_user_id": r.reported_user_id,
        "reason": r.reason.value,
        "details": r.details,
        "status": r.status.value,
        "created_at": _iso(getattr(r, "created_at", None)),
    }


# ---- publications ------------------------------------------------------------------------------------------
def _item_detail(db: Session, item: Item) -> dict[str, Any]:
    owner = db.get(User, item.owner_id)
    zone = db.get(Zone, item.zone_id) if item.zone_id else None
    offers = db.execute(select(Offer).where(Offer.item_id == item.id).order_by(Offer.id.desc()).limit(50)).scalars()
    reports = db.execute(select(Report).where(Report.item_id == item.id).order_by(Report.id.desc())).scalars()
    posts = db.execute(select(ChannelPost).where(ChannelPost.item_id == item.id).order_by(ChannelPost.id.desc()))
    codes = {item.id: item.public_id}
    return {
        **_item_out(item),
        "description": item.description,
        "zone": zone.name if zone else None,
        "accepts_swap": item.accepts_swap,
        "has_damage": item.has_damage,
        "works": item.works,
        "channel_status": item.channel_status.value,
        "score_breakdown": item.score_breakdown,
        "images": len(item.images),
        "owner": _user_out(owner) if owner else None,
        "offers": [offer_out(o, codes) for o in offers],
        "reports": [_report_out(r) for r in reports],
        "channel_posts": [
            {"id": p.id, "status": p.status.value, "posted_at": _iso(p.posted_at), "removal_reason": p.removal_reason}
            for p in posts.scalars()
        ],
    }


@router.get("/items/{public_id}")
def item_detail(public_id: str, db: Session = Depends(get_db), _: AdminPrincipal = Depends(VIEWER)) -> dict:
    return _item_detail(db, pubs.get_by_public_id(db, public_id))


class ItemEditIn(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=120)
    category: str | None = Field(None, min_length=2, max_length=60)
    description: str | None = Field(None, max_length=2000)
    reason: str = Field(..., min_length=3, max_length=200)

    @model_validator(mode="after")
    def _something(self) -> ItemEditIn:
        if self.title is None and self.category is None and self.description is None:
            raise ValueError("nothing to change")
        return self


@router.patch("/items/{public_id}")
def edit_item(
    public_id: str, body: ItemEditIn, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(MODERATOR)
) -> dict:
    """Moderation edit of the texts (e.g. remove inappropriate wording). Price and status are never edited here."""
    item = pubs.get_by_public_id(db, public_id, for_update=True)
    if item.status in CLOSED:
        raise InvalidState("closed publications cannot be edited", code="ITEM_CLOSED")
    changes = {k: v for k, v in body.model_dump(exclude={"reason"}).items() if v is not None}
    found = detect(" ".join(str(v) for v in changes.values()))
    if found.detected:
        raise ValidationFailed(
            "the new text contains contact data", code="CONTACT_DATA_IN_TEXT", details={"kinds": found.kinds}
        )
    before = {k: getattr(item, k) for k in changes}
    for k, v in changes.items():
        setattr(item, k, v.strip() if isinstance(v, str) else v)
    pubs._refresh_search_text(item)
    db.flush()
    audit(
        db,
        AuditAction.ADMIN_EDITED_ITEM,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        entity_type="item",
        entity_id=item.public_id,
        details={"fields": sorted(changes), "before": before, "reason": body.reason},
    )
    return _item_detail(db, item)


# ---- operations (transactions) -------------------------------------------------------------------------------
def tx_out(t: Transaction, items: dict[int, Item], offer_codes: dict[int, str]) -> dict[str, Any]:
    it = items.get(t.item_id)
    return {
        "transaction_id": t.public_id,
        "item_id": t.item_id,
        "item_code": it.public_id if it else None,
        "item_title": it.title if it else None,
        "offer_id": offer_codes.get(t.offer_id),
        "owner_id": t.owner_id,
        "bidder_id": t.bidder_id,
        "status": t.status.value,
        "amount": str(t.amount),
        "currency": t.currency,
        "created_at": _iso(getattr(t, "created_at", None)),
        "payment_due_at": _iso(t.payment_due_at),
        "paid_at": _iso(t.paid_at),
        "released_at": _iso(t.released_at),
        "closed_at": _iso(t.closed_at),
        "close_reason": t.close_reason,
        "contact_released": t.released_at is not None,
        "keyword_sent": t.security_keyword is not None and t.released_at is not None,  # the keyword is never shown
    }


@router.get("/transactions/{public_id}")
def transaction_detail(public_id: str, db: Session = Depends(get_db), _: AdminPrincipal = Depends(VIEWER)) -> dict:
    t = db.execute(select(Transaction).where(Transaction.public_id == public_id.strip().upper())).scalar_one_or_none()
    if t is None:
        raise NotFound("transaction not found", code="TRANSACTION_NOT_FOUND")
    item = db.get(Item, t.item_id)
    offer = db.get(Offer, t.offer_id)
    owner, bidder = db.get(User, t.owner_id), db.get(User, t.bidder_id)
    payments = db.execute(select(Payment).where(Payment.transaction_id == t.id).order_by(Payment.id)).scalars()
    notes = db.execute(
        select(Notification.kind, Notification.status, Notification.created_at, Notification.sent_at)
        .where(Notification.user_id.in_([t.owner_id, t.bidder_id]), Notification.kind.like("CONTACT_RELEASED%"))
        .order_by(Notification.id)
    ).all()
    return {
        **tx_out(t, {item.id: item} if item else {}, {offer.id: offer.public_id} if offer else {}),
        "offer": offer_out(offer, {item.id: item.public_id} if item else {}) if offer else None,
        "owner": _user_out(owner) if owner else None,
        "bidder": _user_out(bidder) if bidder else None,
        "payments": [payment_out(p, {t.id: t.public_id}) for p in payments],
        "release_notifications": [
            {"kind": k, "status": s.value, "created_at": _iso(c), "sent_at": _iso(sa)} for k, s, c, sa in notes
        ],
    }


def payment_out(p: Payment, tx_codes: dict[int, str]) -> dict[str, Any]:
    return {
        "id": p.id,
        "transaction_id": p.transaction_id,
        "transaction_code": tx_codes.get(p.transaction_id),
        "provider": p.provider.value,
        "provider_payment_id": p.provider_payment_id,
        "status": p.status.value,
        "provider_status": p.provider_status,
        "amount": str(p.amount),
        "currency": p.currency,
        "live_mode": p.live_mode,
        "created_at": _iso(p.created_at),
        "verified_at": _iso(p.verified_at),
    }


# ---- conversations ------------------------------------------------------------------------------------------
@router.get("/conversations")
def list_conversations(
    q: str | None = Query(None, max_length=60, description="exact WhatsApp number"),
    user_id: int | None = None,
    page: tuple[int, int] = Depends(_page),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(MODERATOR),
) -> list[dict]:
    """Who talked to the bot recently (metadata only; opening a conversation is audited)."""
    stats = (
        select(Message.user_id, func.count().label("n"), func.max(Message.created_at).label("last"))
        .group_by(Message.user_id)
        .subquery()
    )
    stmt = select(User, stats.c.n, stats.c.last).join(stats, stats.c.user_id == User.id)
    if user_id:
        stmt = stmt.where(User.id == user_id)
    if q:
        try:
            stmt = stmt.where(User.whatsapp_hash == pii.whatsapp_hash(q))
        except ValidationFailed:
            return []
    rows = db.execute(stmt.order_by(stats.c.last.desc()).limit(page[0]).offset(page[1])).all()
    sessions = {
        s.user_id: s
        for s in db.execute(
            select(ConversationSession).where(
                ConversationSession.user_id.in_([u.id for u, _, _ in rows]), ConversationSession.active.is_(True)
            )
        ).scalars()
    }
    blocked = dict(
        db.execute(
            select(Message.user_id, func.count())
            .where(Message.blocked.is_(True), Message.user_id.in_([u.id for u, _, _ in rows]))
            .group_by(Message.user_id)
        ).all()
    )
    return [
        {
            **_user_out(u),
            "messages": n,
            "blocked_messages": blocked.get(u.id, 0),
            "last_message_at": _iso(last),
            "state": sessions[u.id].conversation_state if u.id in sessions else None,
            "intent": sessions[u.id].current_intent if u.id in sessions else None,
        }
        for u, n, last in rows
    ]


@router.get("/conversations/{user_id}/context")
def conversation_context(user_id: int, db: Session = Depends(get_db), _: AdminPrincipal = Depends(MODERATOR)) -> dict:
    """Current conversation context: state, item, offer and operation (codes only, no message bodies)."""
    s = db.execute(
        select(ConversationSession).where(ConversationSession.user_id == user_id, ConversationSession.active.is_(True))
    ).scalar_one_or_none()
    if s is None:
        return {"user_id": user_id, "state": None}
    items = _codes(db, Item, {s.current_item_id or 0, s.selected_item_id or 0})
    offers = _codes(db, Offer, {s.current_offer_id or 0})
    txs = _codes(db, Transaction, {s.transaction_id or 0})
    return {
        "user_id": user_id,
        "state": s.conversation_state,
        "intent": s.current_intent,
        "current_item": items.get(s.current_item_id),
        "selected_item": items.get(s.selected_item_id),
        "offer": offers.get(s.current_offer_id),
        "transaction": txs.get(s.transaction_id),
        "last_inbound_at": _iso(s.last_inbound_at),
    }


# ---- security ------------------------------------------------------------------------------------------------
@router.get("/security/warnings")
def list_warnings(
    user_id: int | None = None,
    page: tuple[int, int] = Depends(_page),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(VIEWER),
) -> list[dict]:
    stmt = select(Warning)
    if user_id:
        stmt = stmt.where(Warning.user_id == user_id)
    return [
        {
            "id": w.id,
            "user_id": w.user_id,
            "reason": w.reason,
            "message_sent": w.message_sent,
            "security_event_id": w.security_event_id,
            "created_at": _iso(w.created_at),
        }
        for w in db.execute(stmt.order_by(Warning.id.desc()).limit(page[0]).offset(page[1])).scalars()
    ]


@router.get("/audit")
def list_audit(
    action: str | None = Query(None, max_length=60),
    entity_type: str | None = Query(None, max_length=40),
    entity_id: str | None = Query(None, max_length=40),
    page: tuple[int, int] = Depends(_page),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(ADMIN),
) -> list[dict]:
    """Append-only audit trail (details are sanitised when written: no secrets, no keywords)."""
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action.upper())
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    return [
        {
            "id": a.id,
            "action": a.action,
            "actor_type": a.actor_type.value,
            "actor_id": a.actor_id,
            "entity_type": a.entity_type,
            "entity_id": a.entity_id,
            "details": a.details,
            "created_at": _iso(a.created_at),
        }
        for a in db.execute(stmt.order_by(AuditLog.id.desc()).limit(page[0]).offset(page[1])).scalars()
    ]


# ---- user profile extras -----------------------------------------------------------------------------------
def user_activity(db: Session, user_id: int) -> dict[str, Any]:
    offers = list(
        db.execute(
            select(Offer)
            .where(or_(Offer.bidder_id == user_id, Offer.owner_id == user_id))
            .order_by(Offer.id.desc())
            .limit(50)
        ).scalars()
    )
    codes = _codes(db, Item, {o.item_id for o in offers})
    item_ids = select(Item.id).where(Item.owner_id == user_id)
    reports = db.execute(
        select(Report)
        .where(or_(Report.reported_user_id == user_id, Report.item_id.in_(item_ids), Report.reporter_id == user_id))
        .order_by(Report.id.desc())
        .limit(50)
    ).scalars()
    return {"offers": [offer_out(o, codes) for o in offers], "reports": [_report_out(r) for r in reports]}
