"""Channel service with MANUAL administration.

The official WhatsApp Cloud API cannot publish to Channels, so (user decision) the admin posts to the
Channel by hand. The backend:
* computes the attractiveness score and offers a diversified "suggested items" list;
* records manual posts (channel_posts / channel_exposures);
* reacts to ITEM_COMPLETED / ITEM_EXPIRED / ITEM_DELETED by flagging live posts REMOVAL_PENDING so the
  admin removes them (the Channel is never the source of truth; offers always re-check PostgreSQL);
* reconciliation detects posts of closed items that were not flagged.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import Conflict, InvalidState, NotFound
from app.models.audit import DomainEvent
from app.models.channel import ChannelExposure, ChannelPost
from app.models.enums import LIVE_CHANNEL_POST_STATUSES, ActorType, ChannelPostStatus, ItemStatus
from app.models.item import Item
from app.services.audit.service import AuditAction, audit
from app.services.channel.score import attractiveness
from app.services.system import config_service as cfg

CLOSING_EVENTS = ("ITEM_COMPLETED", "ITEM_EXPIRED", "ITEM_DELETED")


def flag_item_posts_for_removal(session: Session, item_id: int, reason: str) -> int:
    posts = (
        session.execute(
            select(ChannelPost)
            .where(
                ChannelPost.item_id == item_id,
                ChannelPost.status.in_(
                    [s for s in LIVE_CHANNEL_POST_STATUSES if s != ChannelPostStatus.REMOVAL_PENDING]
                ),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for p in posts:
        if p.status == ChannelPostStatus.PENDING:
            # Never posted: nothing to remove from WhatsApp.
            p.status = ChannelPostStatus.REMOVED
            p.removed_at = now
        else:
            p.status = ChannelPostStatus.REMOVAL_PENDING
        p.removal_requested_at = now
        p.removal_reason = reason
        audit(
            session,
            AuditAction.CHANNEL_POST_REMOVAL_REQUESTED,
            actor_type=ActorType.SYSTEM,
            entity_type="channel_post",
            entity_id=p.id,
            details={"item_id": item_id, "reason": reason},
        )
    session.flush()
    return len(posts)


def _on_item_closed(session: Session, event: DomainEvent) -> None:
    item = session.execute(select(Item).where(Item.public_id == event.aggregate_id)).scalar_one_or_none()
    if item is not None:
        flag_item_posts_for_removal(session, item.id, event.event_type)


def register_event_handlers(bus: Any) -> None:
    for et in CLOSING_EVENTS:
        bus.subscribe(et, _on_item_closed)


# ---- admin facing -------------------------------------------------------------------------------
def suggestions(session: Session, limit: int = 10) -> list[dict[str, Any]]:
    """Diversified list of attractive PUBLISHED items for the admin to post manually."""
    now = utcnow()
    min_score = int(cfg.get_value(session, "channel_min_score"))
    max_exp = int(cfg.get_value(session, "channel_max_exposure_per_item"))
    min_gap = timedelta(hours=int(cfg.get_value(session, "channel_min_hours_between_same_item")))
    live_ids = set(
        session.execute(select(ChannelPost.item_id).where(ChannelPost.status.in_(LIVE_CHANNEL_POST_STATUSES))).scalars()
    )
    exposures = dict(
        session.execute(select(ChannelExposure.item_id, func.count()).group_by(ChannelExposure.item_id)).all()
    )
    last_exp = dict(
        session.execute(
            select(ChannelExposure.item_id, func.max(ChannelExposure.exposed_at)).group_by(ChannelExposure.item_id)
        ).all()
    )
    recent = dict(
        session.execute(
            select(ChannelExposure.item_id, func.count())
            .where(ChannelExposure.exposed_at > now - timedelta(days=7))
            .group_by(ChannelExposure.item_id)
        ).all()
    )
    items = (
        session.execute(select(Item).where(Item.status == ItemStatus.PUBLISHED, Item.expires_at > now)).scalars().all()
    )
    scored = []
    for it in items:
        if it.id in live_ids or exposures.get(it.id, 0) >= max_exp:
            continue
        if last_exp.get(it.id) and now - last_exp[it.id] < min_gap:
            continue
        score = attractiveness(it, now, recent_exposures=recent.get(it.id, 0))
        it.attractiveness_score = score
        bonus = 10 if it.featured else 0
        if score + bonus >= min_score or it.featured and score >= min_score:
            scored.append((score + bonus, score, it))
    scored.sort(key=lambda t: (-t[0], t[2].id))
    # Diversity: round-robin over categories and avoid the same zone/category back-to-back.
    picked: list[tuple[int, Item]] = []
    used_cat: dict[str | None, int] = {}
    for _, score, it in scored:
        if len(picked) >= limit:
            break
        if used_cat.get(it.category, 0) >= max(1, limit // 3):
            continue
        if picked and picked[-1][1].category == it.category and picked[-1][1].zone_id == it.zone_id:
            continue
        picked.append((score, it))
        used_cat[it.category] = used_cat.get(it.category, 0) + 1
    session.flush()
    return [
        {
            "item_id": it.public_id,
            "title": it.title,
            "category": it.category,
            "score": score,
            "featured": it.featured,
            "price": str(it.base_price),
            "accepts_swap": it.accepts_swap,
        }
        for score, it in picked
    ]


def register_manual_post(
    session: Session, admin_id: int, item_public_id: str, *, external_ref: str | None, position: int | None = None
) -> ChannelPost:
    item = session.execute(
        select(Item)
        .where(Item.public_id == item_public_id.upper())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if item is None:
        raise NotFound("item not found", code="ITEM_NOT_FOUND")
    if item.status != ItemStatus.PUBLISHED:
        raise InvalidState("only PUBLISHED items can be posted to the channel", code="ITEM_NOT_PUBLISHED")
    live = session.execute(
        select(ChannelPost.id).where(ChannelPost.item_id == item.id, ChannelPost.status.in_(LIVE_CHANNEL_POST_STATUSES))
    ).first()
    if live:
        raise Conflict("item already has a live channel post", code="CHANNEL_POST_EXISTS")
    now = utcnow()
    score = attractiveness(item, now)
    post = ChannelPost(
        item_id=item.id,
        status=ChannelPostStatus.PUBLISHED,
        score_at_post=score,
        external_ref=(external_ref or "")[:255] or None,
        posted_at=now,
        posted_by_admin_id=admin_id,
    )
    session.add(post)
    session.flush()
    session.add(
        ChannelExposure(
            item_id=item.id, channel_post_id=post.id, score=score, position=position, status="PUBLISHED", exposed_at=now
        )
    )
    session.flush()
    audit(
        session,
        AuditAction.CHANNEL_POST_CREATED,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="channel_post",
        entity_id=post.id,
        details={"item": item.public_id, "manual": True},
    )
    return post


def confirm_removed(session: Session, admin_id: int, post_id: int) -> ChannelPost:
    post = session.execute(
        select(ChannelPost).where(ChannelPost.id == post_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if post is None:
        raise NotFound("post not found", code="CHANNEL_POST_NOT_FOUND")
    if post.status == ChannelPostStatus.REMOVED:
        return post
    now = utcnow()
    if post.status != ChannelPostStatus.REMOVAL_PENDING:
        post.removal_requested_at = now
        post.removal_reason = "ADMIN"
    post.status = ChannelPostStatus.REMOVED
    post.removed_at = now
    post.removed_by_admin_id = admin_id
    session.flush()
    audit(
        session,
        AuditAction.CHANNEL_POST_REMOVED,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="channel_post",
        entity_id=post.id,
    )
    return post


def reconcile(session: Session) -> dict[str, int]:
    """Live posts whose item is no longer PUBLISHED/RESERVED -> REMOVAL_PENDING. Logged per correction."""
    rows = (
        session.execute(
            select(ChannelPost.item_id)
            .join(Item, Item.id == ChannelPost.item_id)
            .where(
                ChannelPost.status.in_(
                    [s for s in LIVE_CHANNEL_POST_STATUSES if s != ChannelPostStatus.REMOVAL_PENDING]
                ),
                Item.status.in_([ItemStatus.COMPLETED, ItemStatus.EXPIRED, ItemStatus.DELETED]),
            )
        )
        .scalars()
        .all()
    )
    fixed = 0
    for item_id in set(rows):
        fixed += flag_item_posts_for_removal(session, item_id, "RECONCILIATION")
    pending = session.execute(
        select(func.count()).select_from(ChannelPost).where(ChannelPost.status == ChannelPostStatus.REMOVAL_PENDING)
    ).scalar_one()
    if fixed:
        audit(
            session,
            AuditAction.CHANNEL_RECONCILIATION,
            actor_type=ActorType.WORKER,
            details={"flagged": fixed, "removal_pending": pending},
        )
    return {"flagged": fixed, "removal_pending": pending}


def set_featured(session: Session, admin_id: int, item_public_id: str, featured: bool) -> Item:
    item = session.execute(
        select(Item)
        .where(Item.public_id == item_public_id.upper())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if item is None:
        raise NotFound("item not found", code="ITEM_NOT_FOUND")
    item.featured = featured
    session.flush()
    audit(
        session,
        AuditAction.ADMIN_CHANNEL_ACTION,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="item",
        entity_id=item.public_id,
        details={"featured": featured},
    )
    return item
