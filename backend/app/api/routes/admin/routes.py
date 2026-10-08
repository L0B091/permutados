"""Admin console API (consumed by the Android app). Everything passes through the backend and is audited."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import AdminPrincipal, client_ip, get_db, require_role
from app.core.errors import NotFound
from app.models.audit import AuditLog
from app.models.channel import ChannelPost
from app.models.conversation import ConversationSession, Message
from app.models.enums import (
    ActorType,
    AdminRole,
    BanStatus,
    ChannelPostStatus,
    ItemStatus,
    OfferStatus,
    PaymentStatus,
    ReportStatus,
    SystemMode,
    TransactionStatus,
    UserStatus,
)
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.payment import Payment
from app.models.report import Report
from app.models.security import Ban, SecurityEvent, Warning
from app.models.user import User
from app.services.admin import service as admin_service
from app.services.audit.service import AuditAction, audit
from app.services.channel import service as channel
from app.services.publications import service as pubs
from app.services.system import config_service as cfg

router = APIRouter(prefix="/admin", tags=["admin"])
VIEWER = require_role(AdminRole.VIEWER)
MODERATOR = require_role(AdminRole.MODERATOR)
ADMIN = require_role(AdminRole.ADMIN)


def _page(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100_000)) -> tuple[int, int]:
    return limit, offset


def _user_out(u: User) -> dict[str, Any]:
    return {
        "id": u.id,
        "whatsapp_id": u.whatsapp_id,
        "display_name": u.display_name,
        "status": u.status.value,
        "warnings_count": u.warnings_count,
        "evasion_strikes": u.evasion_strikes,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def _item_out(i: Item) -> dict[str, Any]:
    return {
        "item_id": i.public_id,
        "owner_id": i.owner_id,
        "status": i.status.value,
        "title": i.title,
        "category": i.category,
        "price": str(i.base_price) if i.base_price is not None else None,
        "currency": i.currency,
        "featured": i.featured,
        "score": i.attractiveness_score,
        "published_at": i.published_at.isoformat() if i.published_at else None,
        "expires_at": i.expires_at.isoformat() if i.expires_at else None,
    }


# ---- dashboard -----------------------------------------------------------------------------------
@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), _: AdminPrincipal = Depends(VIEWER)) -> dict:
    def count(model, *where):
        return db.execute(select(func.count()).select_from(model).where(*where)).scalar_one()

    revenue = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(Payment.status == PaymentStatus.APPROVED)
    ).scalar_one()
    return {
        "users": count(User),
        "users_suspended": count(User, User.status == UserStatus.SUSPENDED),
        "users_banned": count(User, User.status == UserStatus.BANNED),
        "items_published": count(Item, Item.status == ItemStatus.PUBLISHED),
        "items_reserved": count(Item, Item.status == ItemStatus.RESERVED),
        "offers_pending": count(Offer, Offer.status == OfferStatus.PENDING),
        "offers_accepted_unpaid": count(Offer, Offer.status == OfferStatus.PAYMENT_PENDING),
        "contacts_released": count(Transaction, Transaction.status == TransactionStatus.CONTACT_RELEASED),
        "payments_approved": count(Payment, Payment.status == PaymentStatus.APPROVED),
        "payments_needing_refund": count(Payment, Payment.status == PaymentStatus.NEEDS_REFUND),
        "revenue": str(revenue),
        "bans_pending_review": count(Ban, Ban.status == BanStatus.PENDING_REVIEW),
        "reports_open": count(Report, Report.status == ReportStatus.OPEN),
        "channel_removal_pending": count(ChannelPost, ChannelPost.status == ChannelPostStatus.REMOVAL_PENDING),
        "system_mode": cfg.current_mode(db).value,
    }


# ---- users -----------------------------------------------------------------------------------------
@router.get("/users")
def list_users(
    q: str | None = Query(None, max_length=60),
    status: UserStatus | None = None,
    page: tuple[int, int] = Depends(_page),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(VIEWER),
) -> list[dict]:
    stmt = select(User)
    if q:
        stmt = stmt.where((User.whatsapp_id.contains(q)) | (User.display_name.ilike(f"%{q}%")))
    if status:
        stmt = stmt.where(User.status == status)
    rows = db.execute(stmt.order_by(User.id.desc()).limit(page[0]).offset(page[1])).scalars()
    return [_user_out(u) for u in rows]


@router.get("/users/{user_id}")
def get_user(
    user_id: int, request: Request, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(VIEWER)
) -> dict:
    u = admin_service.find_user(db, user_id)
    audit(
        db,
        AuditAction.ADMIN_VIEWED_USER,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        entity_type="user",
        entity_id=u.id,
        ip_address=client_ip(request),
    )
    warnings = db.execute(select(Warning).where(Warning.user_id == u.id).order_by(Warning.id.desc())).scalars()
    bans = db.execute(select(Ban).where(Ban.user_id == u.id).order_by(Ban.id.desc())).scalars()
    items = db.execute(select(Item).where(Item.owner_id == u.id).order_by(Item.id.desc()).limit(50)).scalars()
    return {
        **_user_out(u),
        "warnings": [{"id": w.id, "reason": w.reason, "created_at": w.created_at.isoformat()} for w in warnings],
        "bans": [{"id": b.id, "status": b.status.value, "reason": b.reason} for b in bans],
        "items": [_item_out(i) for i in items],
    }


class ReasonIn(BaseModel):
    reason: str = Field(..., min_length=3, max_length=200)


class NotesIn(BaseModel):
    notes: str | None = Field(None, max_length=1000)
    reset_strikes: bool = False


@router.post("/users/{user_id}/suspend")
def suspend(
    user_id: int, body: ReasonIn, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(MODERATOR)
) -> dict:
    return _user_out(admin_service.suspend_user(db, admin.id, user_id, body.reason))


@router.post("/users/{user_id}/ban")
def ban(user_id: int, body: NotesIn, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(MODERATOR)) -> dict:
    return _user_out(admin_service.confirm_ban(db, admin.id, user_id, body.notes))


@router.post("/users/{user_id}/reinstate")
def reinstate(
    user_id: int, body: NotesIn, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(ADMIN)
) -> dict:
    return _user_out(admin_service.reinstate(db, admin.id, user_id, body.notes, reset_strikes=body.reset_strikes))


@router.get("/conversations/{user_id}")
def conversation(
    user_id: int,
    request: Request,
    page: tuple[int, int] = Depends(_page),
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(MODERATOR),
) -> dict:
    u = admin_service.find_user(db, user_id)
    audit(
        db,
        AuditAction.ADMIN_VIEWED_CONVERSATION,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        entity_type="user",
        entity_id=u.id,
        ip_address=client_ip(request),
    )
    conv = db.execute(
        select(ConversationSession).where(ConversationSession.user_id == u.id, ConversationSession.active.is_(True))
    ).scalar_one_or_none()
    msgs = db.execute(
        select(Message).where(Message.user_id == u.id).order_by(Message.id.desc()).limit(page[0]).offset(page[1])
    ).scalars()
    return {
        "user_id": u.id,
        "state": conv.conversation_state if conv else None,
        "messages": [
            {
                "id": m.id,
                "direction": m.direction.value,
                "type": m.message_type.value,
                "body": m.body,
                "blocked": m.blocked,
                "item_id": m.item_id,
                "offer_id": m.offer_id,
                "transaction_id": m.transaction_id,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ],
    }


# ---- items / offers / transactions / payments --------------------------------------------------
@router.get("/items")
def list_items(
    status: ItemStatus | None = None,
    q: str | None = Query(None, max_length=60),
    page: tuple[int, int] = Depends(_page),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(VIEWER),
) -> list[dict]:
    stmt = select(Item)
    if status:
        stmt = stmt.where(Item.status == status)
    if q:
        stmt = stmt.where((Item.public_id == q.upper()) | Item.title.ilike(f"%{q}%"))
    return [_item_out(i) for i in db.execute(stmt.order_by(Item.id.desc()).limit(page[0]).offset(page[1])).scalars()]


@router.post("/items/{public_id}/remove")
def remove_item(
    public_id: str, body: ReasonIn, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(MODERATOR)
) -> dict:
    item = pubs.admin_remove_item(db, admin.id, public_id, body.reason)
    audit(
        db,
        AuditAction.ADMIN_REMOVED_ITEM,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        entity_type="item",
        entity_id=item.public_id,
        details={"reason": body.reason},
    )
    return _item_out(item)


@router.get("/offers")
def list_offers(
    status: OfferStatus | None = None,
    page: tuple[int, int] = Depends(_page),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(VIEWER),
) -> list[dict]:
    stmt = select(Offer)
    if status:
        stmt = stmt.where(Offer.status == status)
    return [
        {
            "offer_id": o.public_id,
            "item_id": o.item_id,
            "bidder_id": o.bidder_id,
            "owner_id": o.owner_id,
            "type": o.offer_type.value,
            "price": str(o.offered_price) if o.offered_price else None,
            "offered_item": o.offered_item,
            "status": o.status.value,
            "close_reason": o.close_reason.value if o.close_reason else None,
            "created_at": o.created_at.isoformat(),
            "expires_at": o.expires_at.isoformat(),
        }
        for o in db.execute(stmt.order_by(Offer.id.desc()).limit(page[0]).offset(page[1])).scalars()
    ]


@router.get("/transactions")
def list_transactions(
    status: TransactionStatus | None = None,
    page: tuple[int, int] = Depends(_page),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(VIEWER),
) -> list[dict]:
    stmt = select(Transaction)
    if status:
        stmt = stmt.where(Transaction.status == status)
    return [
        {
            "transaction_id": t.public_id,
            "item_id": t.item_id,
            "owner_id": t.owner_id,
            "bidder_id": t.bidder_id,
            "status": t.status.value,
            "amount": str(t.amount),
            "currency": t.currency,
            "payment_due_at": t.payment_due_at.isoformat(),
            "paid_at": t.paid_at.isoformat() if t.paid_at else None,
            "released_at": t.released_at.isoformat() if t.released_at else None,
        }
        for t in db.execute(stmt.order_by(Transaction.id.desc()).limit(page[0]).offset(page[1])).scalars()
    ]


@router.get("/payments")
def list_payments(
    status: PaymentStatus | None = None,
    page: tuple[int, int] = Depends(_page),
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(VIEWER),
) -> list[dict]:
    stmt = select(Payment)
    if status:
        stmt = stmt.where(Payment.status == status)
    return [
        {
            "id": p.id,
            "transaction_id": p.transaction_id,
            "provider": p.provider.value,
            "provider_payment_id": p.provider_payment_id,
            "status": p.status.value,
            "amount": str(p.amount),
            "currency": p.currency,
            "created_at": p.created_at.isoformat(),
        }
        for p in db.execute(stmt.order_by(Payment.id.desc()).limit(page[0]).offset(page[1])).scalars()
    ]


# ---- security / reports ------------------------------------------------------------------------
@router.get("/security/bans")
def list_bans(
    status: BanStatus | None = BanStatus.PENDING_REVIEW,
    db: Session = Depends(get_db),
    _: AdminPrincipal = Depends(VIEWER),
) -> list[dict]:
    stmt = select(Ban)
    if status:
        stmt = stmt.where(Ban.status == status)
    return [
        {
            "id": b.id,
            "user_id": b.user_id,
            "status": b.status.value,
            "reason": b.reason,
            "created_at": b.created_at.isoformat(),
        }
        for b in db.execute(stmt.order_by(Ban.id.desc())).scalars()
    ]


@router.get("/security/events")
def list_security_events(
    page: tuple[int, int] = Depends(_page), db: Session = Depends(get_db), _: AdminPrincipal = Depends(VIEWER)
) -> list[dict]:
    rows = db.execute(select(SecurityEvent).order_by(SecurityEvent.id.desc()).limit(page[0]).offset(page[1])).scalars()
    return [
        {
            "id": e.id,
            "user_id": e.user_id,
            "type": e.event_type.value,
            "severity": e.severity.value,
            "details": e.details,
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ]


@router.get("/reports")
def list_reports(
    status: ReportStatus | None = ReportStatus.OPEN, db: Session = Depends(get_db), _: AdminPrincipal = Depends(VIEWER)
) -> list[dict]:
    stmt = select(Report)
    if status:
        stmt = stmt.where(Report.status == status)
    return [
        {
            "id": r.id,
            "reporter_id": r.reporter_id,
            "item_id": r.item_id,
            "reported_user_id": r.reported_user_id,
            "reason": r.reason.value,
            "details": r.details,
            "status": r.status.value,
        }
        for r in db.execute(stmt.order_by(Report.id.desc())).scalars()
    ]


class ResolveIn(BaseModel):
    status: ReportStatus
    notes: str | None = Field(None, max_length=1000)


@router.post("/reports/{report_id}/resolve")
def resolve_report(
    report_id: int, body: ResolveIn, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(MODERATOR)
) -> dict:
    from app.core.clock import utcnow

    r = db.get(Report, report_id)
    if r is None:
        raise NotFound("report not found")
    r.status, r.resolution_notes, r.resolved_by_admin_id, r.resolved_at = body.status, body.notes, admin.id, utcnow()
    audit(
        db,
        AuditAction.ADMIN_RESOLVED_REPORT,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        entity_type="report",
        entity_id=r.id,
        details={"status": body.status.value},
    )
    return {"id": r.id, "status": r.status.value}


# ---- configuration / system mode ------------------------------------------------------------------
@router.get("/config")
def get_config(db: Session = Depends(get_db), _: AdminPrincipal = Depends(VIEWER)) -> dict:
    return cfg.get_all(db)


@router.put("/config")
def put_config(body: dict[str, Any], db: Session = Depends(get_db), admin: AdminPrincipal = Depends(ADMIN)) -> dict:
    changed = {}
    for key, value in body.items():
        if key == "system_mode":
            continue  # dedicated endpoint
        before = cfg.get_value(db, key) if key in cfg.VALIDATORS else None
        changed[key] = {"before": before, "after": cfg.set_value(db, key, value, admin_id=admin.id)}
    audit(db, AuditAction.ADMIN_CHANGED_CONFIG, actor_type=ActorType.ADMIN, actor_id=admin.id, details=changed)
    return cfg.get_all(db)


class ModeIn(BaseModel):
    mode: SystemMode
    reason: str = Field(..., min_length=3, max_length=200)


@router.get("/system/mode")
def get_mode(db: Session = Depends(get_db), _: AdminPrincipal = Depends(VIEWER)) -> dict:
    return {"mode": cfg.current_mode(db).value}


@router.put("/system/mode")
def put_mode(body: ModeIn, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(ADMIN)) -> dict:
    before = cfg.current_mode(db).value
    cfg.set_value(db, "system_mode", body.mode.value, admin_id=admin.id)
    audit(
        db,
        AuditAction.ADMIN_CHANGED_SYSTEM_MODE,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        details={"before": before, "after": body.mode.value, "reason": body.reason},
    )
    return {"mode": body.mode.value}


@router.get("/system/health")
def system_health(request: Request, db: Session = Depends(get_db), _: AdminPrincipal = Depends(VIEWER)) -> dict:
    from sqlalchemy import text

    from app.core.config import get_settings
    from app.core.ratelimit import get_rate_limiter

    s = get_settings()
    out: dict[str, Any] = {}
    try:
        db.execute(text("SELECT 1"))
        out["postgresql"] = "ok"
    except Exception:  # pragma: no cover
        out["postgresql"] = "error"
    try:
        get_rate_limiter()._redis.ping()
        out["redis"] = "ok"
    except Exception:
        out["redis"] = "error"
    out["dolphin"] = "configured" if s.dolphin_base_url else "not_configured"
    out["whatsapp"] = "configured" if s.whatsapp_access_token.get_secret_value() else "not_configured"
    out["mercadopago"] = "configured" if getattr(request.app.state, "payment_gateway", None) else "not_configured"
    out["system_mode"] = cfg.current_mode(db).value
    last_job = db.execute(
        select(func.max(AuditLog.created_at)).where(AuditLog.actor_type == ActorType.WORKER)
    ).scalar_one_or_none()
    out["last_worker_activity"] = last_job.isoformat() if last_job else None
    return out


# ---- channel (manual administration) --------------------------------------------------------------
@router.get("/channel/suggestions")
def channel_suggestions(
    limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db), _: AdminPrincipal = Depends(VIEWER)
) -> list[dict]:
    return channel.suggestions(db, limit)


@router.get("/channel/posts")
def channel_posts(
    status: ChannelPostStatus | None = None, db: Session = Depends(get_db), _: AdminPrincipal = Depends(VIEWER)
) -> list[dict]:
    stmt = select(ChannelPost, Item.public_id, Item.status).join(Item, Item.id == ChannelPost.item_id)
    if status:
        stmt = stmt.where(ChannelPost.status == status)
    return [
        {
            "id": p.id,
            "item_id": code,
            "item_status": istatus.value,
            "status": p.status.value,
            "score_at_post": p.score_at_post,
            "external_ref": p.external_ref,
            "posted_at": p.posted_at.isoformat() if p.posted_at else None,
            "removal_reason": p.removal_reason,
        }
        for p, code, istatus in db.execute(stmt.order_by(ChannelPost.id.desc()))
    ]


class ChannelPostIn(BaseModel):
    item_id: str = Field(..., pattern=r"^ITEM-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5,8}$")
    external_ref: str | None = Field(None, max_length=255)
    position: int | None = Field(None, ge=0, le=1000)


@router.post("/channel/posts", status_code=201)
def channel_register(
    body: ChannelPostIn, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(MODERATOR)
) -> dict:
    post = channel.register_manual_post(
        db, admin.id, body.item_id, external_ref=body.external_ref, position=body.position
    )
    audit(
        db,
        AuditAction.ADMIN_CHANNEL_ACTION,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        entity_type="channel_post",
        entity_id=post.id,
        details={"action": "registered", "item": body.item_id},
    )
    return {"id": post.id, "status": post.status.value}


@router.post("/channel/posts/{post_id}/removed")
def channel_removed(post_id: int, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(MODERATOR)) -> dict:
    post = channel.confirm_removed(db, admin.id, post_id)
    audit(
        db,
        AuditAction.ADMIN_CHANNEL_ACTION,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        entity_type="channel_post",
        entity_id=post.id,
        details={"action": "removed"},
    )
    return {"id": post.id, "status": post.status.value}


class FeatureIn(BaseModel):
    featured: bool


@router.post("/items/{public_id}/feature")
def feature(
    public_id: str, body: FeatureIn, db: Session = Depends(get_db), admin: AdminPrincipal = Depends(MODERATOR)
) -> dict:
    return _item_out(channel.set_featured(db, admin.id, public_id, body.featured))


@router.post("/channel/reconcile")
def channel_reconcile(db: Session = Depends(get_db), admin: AdminPrincipal = Depends(MODERATOR)) -> dict:
    res = channel.reconcile(db)
    audit(
        db,
        AuditAction.ADMIN_CHANNEL_ACTION,
        actor_type=ActorType.ADMIN,
        actor_id=admin.id,
        details={"action": "reconcile", **res},
    )
    return res
