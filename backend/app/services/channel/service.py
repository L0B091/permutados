"""WhatsApp Channel service with MANUAL administration (user decision).

The official WhatsApp Cloud API cannot publish to Channels, so the admin posts by hand. The backend does the rest:

* ranks PUBLISHED items with the attractiveness score (``score.py``), applies diversity (section 28) and exposure
  control (section 29) and hands the admin a *suggestions* list - it never posts by itself;
* records what the admin did: a queue of planned posts (PENDING -> PUBLISHED / FAILED -> RETRY_PENDING), direct
  registrations, featured items, removals; every change is audited and writes a ``channel_exposures`` row;
* reacts to ITEM_COMPLETED / ITEM_EXPIRED / ITEM_DELETED events immediately (best-effort event handler) by
  flagging live posts REMOVAL_PENDING, and exposes the "what needs removing" list;
* keeps ``items.channel_status`` (separate from the publication status, section 33) in sync; removing an item from
  the channel never touches the publication;
* reconciles PostgreSQL vs the recorded channel state periodically and fixes what is safe to fix.

The Channel is never the source of truth: offers always re-check PostgreSQL, so a stale post cannot be used to buy
an unavailable item.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import Conflict, InvalidState, NotFound, ValidationFailed
from app.models.audit import DomainEvent
from app.models.channel import ChannelExposure, ChannelPost
from app.models.enums import (
    LIVE_CHANNEL_POST_STATUSES,
    ActorType,
    ChannelPostStatus,
    ItemChannelStatus,
    ItemStatus,
    ReportStatus,
    UserStatus,
)
from app.models.item import Item
from app.models.report import Report
from app.models.user import User
from app.services.audit.service import AuditAction, audit
from app.services.channel.score import compute_scores
from app.services.system import config_service as cfg

CLOSING_EVENTS = ("ITEM_COMPLETED", "ITEM_EXPIRED", "ITEM_DELETED")
BLOCKING_EVENTS = ("USER_SUSPENDED", "USER_BANNED")
CLOSED_ITEM_STATUSES = (ItemStatus.COMPLETED, ItemStatus.EXPIRED, ItemStatus.DELETED)
QUEUED_STATUSES = (ChannelPostStatus.PENDING, ChannelPostStatus.SENDING, ChannelPostStatus.RETRY_PENDING)
STALE_QUEUE = timedelta(hours=24)
FEATURED_BONUS = 15
PRICE_BUCKETS = (20_000, 100_000, 500_000)


# ---- channel_status (separate from publication status) -----------------------------------------------
def derive_channel_status(statuses: list[tuple[ChannelPostStatus, datetime | None]]) -> ItemChannelStatus:
    live = {s for s, _ in statuses}
    if ChannelPostStatus.REMOVAL_PENDING in live:
        return ItemChannelStatus.REMOVAL_PENDING
    if ChannelPostStatus.PUBLISHED in live:
        return ItemChannelStatus.VISIBLE
    if live & set(QUEUED_STATUSES):
        return ItemChannelStatus.QUEUED
    if any(s == ChannelPostStatus.REMOVED and posted for s, posted in statuses):
        return ItemChannelStatus.REMOVED
    return ItemChannelStatus.NOT_POSTED


def expected_channel_status(session: Session, item_id: int) -> ItemChannelStatus:
    rows = session.execute(
        select(ChannelPost.status, ChannelPost.posted_at).where(ChannelPost.item_id == item_id)
    ).all()
    return derive_channel_status([(s, p) for s, p in rows])


def sync_channel_status(session: Session, item_id: int) -> ItemChannelStatus:
    session.flush()
    status = expected_channel_status(session, item_id)
    item = session.get(Item, item_id)
    if item is not None and item.channel_status != status:
        item.channel_status = status
        session.flush()
    return status


# ---- removal flags (event driven) ---------------------------------------------------------------------
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
        if p.status in QUEUED_STATUSES:
            # Never posted: nothing to remove from WhatsApp.
            p.status = ChannelPostStatus.REMOVED
            p.removed_at = now
        else:
            p.status = ChannelPostStatus.REMOVAL_PENDING
        p.removal_requested_at = now
        p.removal_reason = reason[:40]
        audit(
            session,
            AuditAction.CHANNEL_POST_REMOVAL_REQUESTED,
            actor_type=ActorType.SYSTEM,
            entity_type="channel_post",
            entity_id=p.id,
            details={"item_id": item_id, "reason": reason},
        )
    if posts:
        sync_channel_status(session, item_id)
    session.flush()
    return len(posts)


def _on_item_closed(session: Session, event: DomainEvent) -> None:
    item = session.execute(select(Item).where(Item.public_id == event.aggregate_id)).scalar_one_or_none()
    if item is not None:
        flag_item_posts_for_removal(session, item.id, event.event_type)


def _on_user_blocked(session: Session, event: DomainEvent) -> None:
    """Suspended/banned owner: their visible posts must come down (the items stay as they are)."""
    item_ids = set(
        session.execute(
            select(ChannelPost.item_id)
            .join(Item, Item.id == ChannelPost.item_id)
            .where(Item.owner_id == int(event.aggregate_id), ChannelPost.status.in_(LIVE_CHANNEL_POST_STATUSES))
        ).scalars()
    )
    for item_id in item_ids:
        flag_item_posts_for_removal(session, item_id, "OWNER_BLOCKED")


def register_event_handlers(bus: Any) -> None:
    for et in CLOSING_EVENTS:
        bus.subscribe(et, _on_item_closed)  # best effort: retried by the worker, backed by reconciliation
    for et in BLOCKING_EVENTS:
        bus.subscribe(et, _on_user_blocked)


# ---- scores ---------------------------------------------------------------------------------------------
def _decay_hours(session: Session) -> float:
    return float(cfg.get_value(session, "channel_exposure_decay_hours"))


def refresh_scores(session: Session, now: datetime | None = None) -> int:
    """Recompute and store the score + breakdown of every active item (admin view / metrics)."""
    now = now or utcnow()
    items = (
        session.execute(select(Item).where(Item.status.in_([ItemStatus.PUBLISHED, ItemStatus.RESERVED])))
        .scalars()
        .all()
    )
    results = compute_scores(session, list(items), now, decay_hours=_decay_hours(session))
    changed = 0
    for it in items:
        r = results[it.id]
        if it.attractiveness_score != r.score:
            changed += 1
        it.attractiveness_score = r.score
        it.score_breakdown = r.breakdown()
        it.score_updated_at = now
    session.flush()
    return changed


# ---- exposure control -------------------------------------------------------------------------------------
def quota(session: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    daily = int(cfg.get_value(session, "channel_daily_limit"))
    gap = timedelta(minutes=int(cfg.get_value(session, "channel_min_minutes_between_posts")))
    posted_24h = session.execute(
        select(func.count()).select_from(ChannelPost).where(ChannelPost.posted_at > now - timedelta(hours=24))
    ).scalar_one()
    queued = session.execute(
        select(func.count()).select_from(ChannelPost).where(ChannelPost.status.in_(QUEUED_STATUSES))
    ).scalar_one()
    last = session.execute(select(func.max(ChannelPost.posted_at))).scalar_one_or_none()
    next_at = last + gap if last else None
    return {
        "daily_limit": daily,
        "posted_last_24h": posted_24h,
        "queued": queued,
        "remaining": max(0, daily - posted_24h - queued),
        "last_post_at": last.isoformat() if last else None,
        "next_post_allowed_at": next_at.isoformat() if next_at and next_at > now else None,
        "min_minutes_between_posts": int(gap.total_seconds() // 60),
    }


def _price_bucket(price) -> int:
    p = float(price or 0)
    return sum(p >= b for b in PRICE_BUCKETS)


def _age_bucket(it: Item, now: datetime) -> int:
    days = (now - (it.published_at or now)).total_seconds() / 86400
    return 0 if days < 2 else 1 if days < 7 else 2


def diversify(
    candidates: list[dict[str, Any]],
    limit: int,
    *,
    category: bool = True,
    zone: bool = True,
    price: bool = True,
) -> list[dict[str, Any]]:
    """Greedy re-ranking: attractiveness + diversity + rotation (section 28).

    Each candidate dict has ``priority`` plus the attributes ``category``, ``zone_id``, ``price_bucket``, ``swap``,
    ``age_bucket`` and ``owner_id``. Penalties grow with how many already-picked items share an attribute; the
    same category never appears twice in a row (unless nothing else is left), at most one item per owner and, with
    category diversity on, at most ceil(limit/3) items per category (hard cap: the list may come out shorter)."""
    picked: list[dict[str, Any]] = []
    remaining = sorted(candidates, key=lambda c: (-c["priority"], c["item_pk"]))
    cat_cap = max(1, math.ceil(limit / 3))
    owners: set[int] = set()

    def penalty(c: dict[str, Any]) -> float:
        same = lambda key: sum(1 for p in picked if p[key] == c[key])  # noqa: E731
        pen = 0.0
        if category:
            pen += 12 * same("category")
        if zone:
            pen += 6 * same("zone_id")
        if price:
            pen += 6 * same("price_bucket")
        pen += 4 * same("swap") + 4 * same("age_bucket")
        return pen

    while remaining and len(picked) < limit:

        def allowed(c: dict[str, Any], strict: bool) -> bool:
            if c["owner_id"] in owners:
                return False
            if category and sum(1 for p in picked if p["category"] == c["category"]) >= cat_cap:
                return False  # hard cap: a shorter list beats a monotonous one
            return not (strict and category and picked and picked[-1]["category"] == c["category"])

        pool = [c for c in remaining if allowed(c, True)] or [c for c in remaining if allowed(c, False)]
        if not pool:
            break
        best = max(pool, key=lambda c: (c["priority"] - penalty(c), -c["item_pk"]))
        picked.append(best)
        owners.add(best["owner_id"])
        remaining.remove(best)
    return picked


def suggestion_report(session: Session, limit: int = 10, now: datetime | None = None) -> dict[str, Any]:
    """Ranked, diversified suggestions for the admin to post manually (nothing is posted automatically)."""
    now = now or utcnow()
    q = quota(session, now)
    out: dict[str, Any] = {"enabled": bool(cfg.get_value(session, "channel_enabled")), "quota": q, "items": []}
    if not out["enabled"]:
        out["reason"] = "channel_disabled"
        return out
    if q["remaining"] <= 0:
        out["reason"] = "daily_limit_reached"
        return out
    limit = min(limit, q["remaining"])
    min_score = int(cfg.get_value(session, "channel_min_score"))
    max_exp = int(cfg.get_value(session, "channel_max_exposure_per_item"))
    min_gap = timedelta(hours=int(cfg.get_value(session, "channel_min_hours_between_same_item")))
    featured_max = int(cfg.get_value(session, "channel_featured_max_per_batch"))
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
    items = list(
        session.execute(
            select(Item)
            .join(User, User.id == Item.owner_id)
            .where(Item.status == ItemStatus.PUBLISHED, Item.expires_at > now, User.status == UserStatus.ACTIVE)
        ).scalars()
    )
    scores = compute_scores(session, items, now, decay_hours=_decay_hours(session))
    skipped = {"live_post": 0, "max_exposure": 0, "too_recent": 0, "below_min_score": 0, "unavailable": 0}
    candidates = []
    featured_used = 0
    for it in sorted(items, key=lambda i: (-scores[i.id].score, i.id)):
        r = scores[it.id]
        if not r.available:
            skipped["unavailable"] += 1
            continue
        if it.id in live_ids:
            skipped["live_post"] += 1
            continue
        if max_exp and exposures.get(it.id, 0) >= max_exp:
            skipped["max_exposure"] += 1
            continue
        if last_exp.get(it.id) and now - last_exp[it.id] < min_gap:
            skipped["too_recent"] += 1
            continue
        if r.score < min_score:  # featured items are still subject to the minimum score (section 33)
            skipped["below_min_score"] += 1
            continue
        bonus = 0
        if it.featured and featured_used < featured_max:
            bonus = FEATURED_BONUS
            featured_used += 1
        candidates.append(
            {
                "item_pk": it.id,
                "item": it,
                "score": r.score,
                "priority": r.score + bonus,
                "category": it.category,
                "zone_id": it.zone_id,
                "price_bucket": _price_bucket(it.base_price),
                "swap": bool(it.accepts_swap),
                "age_bucket": _age_bucket(it, now),
                "owner_id": it.owner_id,
                "breakdown": r.breakdown(),
            }
        )
    picked = diversify(
        candidates,
        limit,
        category=bool(cfg.get_value(session, "channel_category_diversity")),
        zone=bool(cfg.get_value(session, "channel_zone_diversity")),
        price=bool(cfg.get_value(session, "channel_price_diversity")),
    )
    out["items"] = [
        {
            "position": n,
            "item_id": c["item"].public_id,
            "title": c["item"].title,
            "category": c["category"],
            "zone_id": c["zone_id"],
            "price": str(c["item"].base_price),
            "accepts_swap": c["item"].accepts_swap,
            "featured": c["item"].featured,
            "score": c["score"],
            "priority": c["priority"],
            "exposures": exposures.get(c["item_pk"], 0),
            "score_breakdown": c["breakdown"],
        }
        for n, c in enumerate(picked, start=1)
    ]
    out["skipped"] = skipped
    out["candidates"] = len(candidates)
    return out


def suggestions(session: Session, limit: int = 10) -> list[dict[str, Any]]:
    return suggestion_report(session, limit)["items"]


# ---- recording what the admin did ------------------------------------------------------------------------
def _lock_item(session: Session, item_public_id: str) -> Item:
    item = session.execute(
        select(Item)
        .where(Item.public_id == item_public_id.strip().upper())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if item is None:
        raise NotFound("item not found", code="ITEM_NOT_FOUND")
    return item


def _lock_post(session: Session, post_id: int) -> ChannelPost:
    post = session.execute(
        select(ChannelPost).where(ChannelPost.id == post_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if post is None:
        raise NotFound("post not found", code="CHANNEL_POST_NOT_FOUND")
    return post


def _assert_postable(session: Session, item: Item) -> None:
    if item.status != ItemStatus.PUBLISHED or (item.expires_at and item.expires_at <= utcnow()):
        raise InvalidState("only PUBLISHED items can be posted to the channel", code="ITEM_NOT_PUBLISHED")
    owner = session.get(User, item.owner_id)
    if owner is None or owner.status != UserStatus.ACTIVE:
        raise InvalidState("the owner's account is not active", code="OWNER_NOT_ACTIVE")
    live = session.execute(
        select(ChannelPost.id).where(ChannelPost.item_id == item.id, ChannelPost.status.in_(LIVE_CHANNEL_POST_STATUSES))
    ).first()
    if live:
        raise Conflict("item already has a live channel post", code="CHANNEL_POST_EXISTS")


def _expose(session: Session, post: ChannelPost, item: Item, score: int, position: int | None, now: datetime) -> None:
    session.add(
        ChannelExposure(
            item_id=item.id, channel_post_id=post.id, score=score, position=position, status="PUBLISHED", exposed_at=now
        )
    )


def queue_posts(session: Session, admin_id: int, item_codes: list[str]) -> list[ChannelPost]:
    """Plan posts (PENDING) from the suggestions; the admin then posts them by hand and confirms each one."""
    if not item_codes:
        raise ValidationFailed("no items", code="NO_ITEMS")
    q = quota(session)
    if len(item_codes) > q["remaining"]:
        raise Conflict("daily channel limit reached", code="CHANNEL_DAILY_LIMIT", details={"remaining": q["remaining"]})
    now = utcnow()
    posts = []
    for pos, code in enumerate(item_codes, start=1):
        item = _lock_item(session, code)
        _assert_postable(session, item)
        score = compute_scores(session, [item], now, decay_hours=_decay_hours(session))[item.id].score
        post = ChannelPost(item_id=item.id, status=ChannelPostStatus.PENDING, score_at_post=score)
        session.add(post)
        session.flush()
        post.external_ref = None
        posts.append(post)
        sync_channel_status(session, item.id)
        audit(
            session,
            AuditAction.ADMIN_CHANNEL_ACTION,
            actor_type=ActorType.ADMIN,
            actor_id=admin_id,
            entity_type="channel_post",
            entity_id=post.id,
            details={"action": "queued", "item": item.public_id, "position": pos, "score": score},
        )
    return posts


def confirm_published(
    session: Session, admin_id: int, post_id: int, *, external_ref: str | None, position: int | None = None
) -> ChannelPost:
    """The admin posted a queued item. The record follows reality: if the item closed in the meantime the post
    is recorded and immediately flagged REMOVAL_PENDING."""
    post = _lock_post(session, post_id)
    if post.status == ChannelPostStatus.PUBLISHED:
        return post  # idempotent
    # A queued post the system cancelled (item closed before it was posted) may still have been posted by hand:
    # the record follows what the admin says happened, and the post is flagged for removal right away.
    cancelled_unposted = post.status == ChannelPostStatus.REMOVED and post.posted_at is None
    if not cancelled_unposted and post.status not in QUEUED_STATUSES:
        raise InvalidState(f"post is {post.status.value}", code="CHANNEL_POST_INVALID_STATE")
    item = session.execute(select(Item).where(Item.id == post.item_id).with_for_update()).scalar_one()
    now = utcnow()
    post.removed_at = None
    post.status = ChannelPostStatus.PUBLISHED
    post.posted_at = now
    post.posted_by_admin_id = admin_id
    post.external_ref = (external_ref or "")[:255] or None
    post.last_error = None
    _expose(session, post, item, post.score_at_post or 0, position, now)
    session.flush()
    audit(
        session,
        AuditAction.CHANNEL_POST_CREATED,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="channel_post",
        entity_id=post.id,
        details={"item": item.public_id, "manual": True, "from_queue": True},
    )
    owner = session.get(User, item.owner_id)
    if item.status in CLOSED_ITEM_STATUSES or (owner is not None and owner.status != UserStatus.ACTIVE):
        flag_item_posts_for_removal(session, item.id, "ITEM_UNAVAILABLE_AT_POST")
    sync_channel_status(session, item.id)
    return post


def mark_failed(session: Session, admin_id: int, post_id: int, error: str) -> ChannelPost:
    post = _lock_post(session, post_id)
    if post.status not in QUEUED_STATUSES:
        raise InvalidState(f"post is {post.status.value}", code="CHANNEL_POST_INVALID_STATE")
    post.status = ChannelPostStatus.FAILED
    post.last_error = (error or "")[:500]
    session.flush()
    audit(
        session,
        AuditAction.CHANNEL_POST_FAILED,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="channel_post",
        entity_id=post.id,
        details={"error": post.last_error},
    )
    sync_channel_status(session, post.item_id)
    return post


def retry_post(session: Session, admin_id: int, post_id: int) -> ChannelPost:
    post = _lock_post(session, post_id)
    if post.status != ChannelPostStatus.FAILED:
        raise InvalidState(f"post is {post.status.value}", code="CHANNEL_POST_INVALID_STATE")
    item = session.execute(select(Item).where(Item.id == post.item_id).with_for_update()).scalar_one()
    _assert_postable(session, item)
    post.status = ChannelPostStatus.RETRY_PENDING
    session.flush()
    audit(
        session,
        AuditAction.ADMIN_CHANNEL_ACTION,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="channel_post",
        entity_id=post.id,
        details={"action": "retry"},
    )
    sync_channel_status(session, post.item_id)
    return post


def register_manual_post(
    session: Session, admin_id: int, item_public_id: str, *, external_ref: str | None, position: int | None = None
) -> ChannelPost:
    """Direct registration of a post the admin already published (without queueing it first)."""
    item = _lock_item(session, item_public_id)
    _assert_postable(session, item)
    now = utcnow()
    score = compute_scores(session, [item], now, decay_hours=_decay_hours(session))[item.id].score
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
    _expose(session, post, item, score, position, now)
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
    sync_channel_status(session, item.id)
    return post


def request_removal(session: Session, admin_id: int, post_id: int, reason: str = "ADMIN") -> ChannelPost:
    """Admin wants a visible post taken down (the publication itself stays as it is)."""
    post = _lock_post(session, post_id)
    if post.status == ChannelPostStatus.REMOVAL_PENDING:
        return post
    if post.status in QUEUED_STATUSES:
        post.status = ChannelPostStatus.REMOVED
        post.removed_at = utcnow()
    elif post.status == ChannelPostStatus.PUBLISHED:
        post.status = ChannelPostStatus.REMOVAL_PENDING
    else:
        raise InvalidState(f"post is {post.status.value}", code="CHANNEL_POST_INVALID_STATE")
    post.removal_requested_at = utcnow()
    post.removal_reason = (reason or "ADMIN")[:40]
    session.flush()
    audit(
        session,
        AuditAction.CHANNEL_POST_REMOVAL_REQUESTED,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="channel_post",
        entity_id=post.id,
        details={"reason": post.removal_reason},
    )
    sync_channel_status(session, post.item_id)
    return post


def confirm_removed(session: Session, admin_id: int, post_id: int) -> ChannelPost:
    post = _lock_post(session, post_id)
    if post.status == ChannelPostStatus.REMOVED:
        return post
    if post.status not in (ChannelPostStatus.PUBLISHED, ChannelPostStatus.REMOVAL_PENDING):
        raise InvalidState(f"post is {post.status.value}", code="CHANNEL_POST_INVALID_STATE")
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
    sync_channel_status(session, post.item_id)
    return post


def set_featured(session: Session, admin_id: int, item_public_id: str, featured: bool) -> Item:
    item = _lock_item(session, item_public_id)
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


# ---- admin views ------------------------------------------------------------------------------------------
def post_view(post: ChannelPost, item: Item, now: datetime, overdue_h: int | None = None) -> dict[str, Any]:
    out = {
        "id": post.id,
        "item_id": item.public_id,
        "item_title": item.title,
        "item_status": item.status.value,
        "channel_status": item.channel_status.value,
        "status": post.status.value,
        "score_at_post": post.score_at_post,
        "external_ref": post.external_ref,
        "posted_at": post.posted_at.isoformat() if post.posted_at else None,
        "removal_reason": post.removal_reason,
        "removal_requested_at": post.removal_requested_at.isoformat() if post.removal_requested_at else None,
        "last_error": post.last_error,
    }
    if overdue_h is not None and post.removal_requested_at:
        age = (now - post.removal_requested_at).total_seconds() / 3600
        out["hours_pending"] = round(age, 1)
        out["overdue"] = age >= overdue_h
    return out


def removals_needed(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """What the admin must delete from the Channel now: REMOVAL_PENDING posts, plus live posts of closed items
    that were not flagged yet (should be empty thanks to events + reconciliation)."""
    now = now or utcnow()
    overdue_h = int(cfg.get_value(session, "channel_removal_overdue_hours"))
    pending = session.execute(
        select(ChannelPost, Item)
        .join(Item, Item.id == ChannelPost.item_id)
        .where(ChannelPost.status == ChannelPostStatus.REMOVAL_PENDING)
        .order_by(ChannelPost.removal_requested_at)
    ).all()
    unflagged = session.execute(
        select(ChannelPost, Item)
        .join(Item, Item.id == ChannelPost.item_id)
        .where(ChannelPost.status == ChannelPostStatus.PUBLISHED, Item.status.in_(CLOSED_ITEM_STATUSES))
    ).all()
    return {
        "removal_pending": [post_view(p, i, now, overdue_h) for p, i in pending],
        "closed_but_visible_unflagged": [post_view(p, i, now) for p, i in unflagged],
        "overdue_hours_threshold": overdue_h,
    }


def overview(session: Session, now: datetime | None = None) -> dict[str, Any]:
    from app.models.worker import JobState

    now = now or utcnow()
    by_status = dict(session.execute(select(ChannelPost.status, func.count()).group_by(ChannelPost.status)).all())
    featured = session.execute(
        select(Item.public_id, Item.title, Item.attractiveness_score).where(
            Item.featured.is_(True), Item.status == ItemStatus.PUBLISHED
        )
    ).all()
    jobs = {
        st.job_name: {
            "status": st.status.value,
            "last_success_at": st.last_success_at.isoformat() if st.last_success_at else None,
            "last_error": st.last_error,
        }
        for st in session.execute(
            select(JobState).where(JobState.job_name.in_(["channel_reconciliation", "refresh_channel_scores"]))
        ).scalars()
    }
    last_report = session.get(JobState, "channel_reconciliation")
    return {
        "mode": "MANUAL",
        "enabled": bool(cfg.get_value(session, "channel_enabled")),
        "posts_by_status": {k.value: v for k, v in by_status.items()},
        "quota": quota(session, now),
        "featured": [{"item_id": c, "title": t, "score": s} for c, t, s in featured],
        "exposures_last_7d": session.execute(
            select(func.count())
            .select_from(ChannelExposure)
            .where(ChannelExposure.exposed_at > now - timedelta(days=7))
        ).scalar_one(),
        "jobs": jobs,
        "last_reconciliation": last_report.last_result if last_report else None,
    }


# ---- reconciliation (section 32) ---------------------------------------------------------------------------
def reconcile(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """Compare PostgreSQL with the recorded channel state. Safe corrections are applied and audited; the rest is
    reported for the admin (the worker stores the report in job_state.last_result)."""
    now = now or utcnow()
    fixed: dict[str, int] = {
        "closed_items_flagged": 0,
        "blocked_owner_flagged": 0,
        "stale_queue_failed": 0,
        "channel_status_corrected": 0,
        "missing_exposures_created": 0,
    }
    issues: dict[str, list[Any]] = {}

    # 1. Closed items still visible -> REMOVAL_PENDING (missed event, crash between steps...).
    closed = set(
        session.execute(
            select(ChannelPost.item_id)
            .join(Item, Item.id == ChannelPost.item_id)
            .where(
                ChannelPost.status.in_(
                    [s for s in LIVE_CHANNEL_POST_STATUSES if s != ChannelPostStatus.REMOVAL_PENDING]
                ),
                Item.status.in_(CLOSED_ITEM_STATUSES),
            )
        ).scalars()
    )
    for item_id in closed:
        fixed["closed_items_flagged"] += flag_item_posts_for_removal(session, item_id, "RECONCILIATION")

    # 2. Live posts of items whose owner is suspended/banned -> REMOVAL_PENDING (safety).
    blocked = set(
        session.execute(
            select(ChannelPost.item_id)
            .join(Item, Item.id == ChannelPost.item_id)
            .join(User, User.id == Item.owner_id)
            .where(ChannelPost.status == ChannelPostStatus.PUBLISHED, User.status != UserStatus.ACTIVE)
        ).scalars()
    )
    for item_id in blocked:
        fixed["blocked_owner_flagged"] += flag_item_posts_for_removal(session, item_id, "OWNER_BLOCKED")

    # 3. Queued for too long (never posted): FAILED, safe because nothing is visible.
    stale = (
        session.execute(
            select(ChannelPost)
            .where(ChannelPost.status.in_(QUEUED_STATUSES), ChannelPost.created_at < now - STALE_QUEUE)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for p in stale:
        p.status = ChannelPostStatus.FAILED
        p.last_error = "not confirmed within 24h (stale queue)"
        fixed["stale_queue_failed"] += 1
        audit(
            session,
            AuditAction.CHANNEL_POST_FAILED,
            actor_type=ActorType.WORKER,
            entity_type="channel_post",
            entity_id=p.id,
            details={"reason": "stale_queue"},
        )
    session.flush()

    # 4. PUBLISHED posts without an exposure record (inconsistent history) -> create it.
    missing = session.execute(
        select(ChannelPost, Item)
        .join(Item, Item.id == ChannelPost.item_id)
        .outerjoin(ChannelExposure, ChannelExposure.channel_post_id == ChannelPost.id)
        .where(ChannelPost.posted_at.is_not(None), ChannelExposure.id.is_(None))
    ).all()
    for p, it in missing:
        _expose(session, p, it, p.score_at_post or 0, None, p.posted_at)
        fixed["missing_exposures_created"] += 1

    # 5. items.channel_status vs channel_posts.
    for post_item_id in set(session.execute(select(ChannelPost.item_id)).scalars()) | set(
        session.execute(select(Item.id).where(Item.channel_status != ItemChannelStatus.NOT_POSTED)).scalars()
    ):
        item = session.get(Item, post_item_id)
        expected = expected_channel_status(session, post_item_id)
        if item is not None and item.channel_status != expected:
            issues.setdefault("channel_status_mismatch", []).append(
                {"item_id": item.public_id, "was": item.channel_status.value, "now": expected.value}
            )
            item.channel_status = expected
            fixed["channel_status_corrected"] += 1
    session.flush()

    # ---- report-only findings ----
    overdue_h = int(cfg.get_value(session, "channel_removal_overdue_hours"))
    overdue = session.execute(
        select(ChannelPost.id, Item.public_id)
        .join(Item, Item.id == ChannelPost.item_id)
        .where(
            ChannelPost.status == ChannelPostStatus.REMOVAL_PENDING,
            ChannelPost.removal_requested_at < now - timedelta(hours=overdue_h),
        )
    ).all()
    if overdue:
        issues["removal_overdue"] = [{"post_id": pid, "item_id": code} for pid, code in overdue]
    dup_refs = session.execute(
        select(ChannelPost.external_ref, func.count())
        .where(ChannelPost.external_ref.is_not(None), ChannelPost.status.in_(LIVE_CHANNEL_POST_STATUSES))
        .group_by(ChannelPost.external_ref)
        .having(func.count() > 1)
    ).all()
    if dup_refs:
        issues["duplicate_external_refs"] = [{"external_ref": r, "posts": n} for r, n in dup_refs]
    max_exp = int(cfg.get_value(session, "channel_max_exposure_per_item"))
    if max_exp:
        over = session.execute(
            select(Item.public_id, func.count())
            .join(ChannelExposure, ChannelExposure.item_id == Item.id)
            .group_by(Item.public_id)
            .having(func.count() > max_exp)
        ).all()
        if over:
            issues["over_exposed"] = [{"item_id": c, "exposures": n} for c, n in over]
    failed = session.execute(
        select(ChannelPost.id, Item.public_id, ChannelPost.last_error)
        .join(Item, Item.id == ChannelPost.item_id)
        .where(ChannelPost.status == ChannelPostStatus.FAILED, ChannelPost.updated_at > now - timedelta(days=7))
    ).all()
    if failed:
        issues["failed_posts"] = [{"post_id": pid, "item_id": c, "error": e} for pid, c, e in failed]
    paused = session.execute(
        select(ChannelPost.id, Item.public_id)
        .join(Item, Item.id == ChannelPost.item_id)
        .where(ChannelPost.status == ChannelPostStatus.PUBLISHED, Item.status == ItemStatus.RESERVED)
    ).all()
    if paused:
        # Not an error (offers re-check PostgreSQL), but the admin should know the item is paused.
        issues["visible_but_paused"] = [{"post_id": pid, "item_id": c} for pid, c in paused]
    reported = session.execute(
        select(ChannelPost.id, Item.public_id)
        .join(Item, Item.id == ChannelPost.item_id)
        .join(Report, Report.item_id == Item.id)
        .where(
            ChannelPost.status == ChannelPostStatus.PUBLISHED,
            Report.status.in_([ReportStatus.OPEN, ReportStatus.REVIEWING]),
        )
    ).all()
    if reported:
        issues["visible_with_open_report"] = [{"post_id": pid, "item_id": c} for pid, c in reported]
    refresh_h = int(cfg.get_value(session, "channel_refresh_hours"))
    stale_visible = session.execute(
        select(ChannelPost.id, Item.public_id)
        .join(Item, Item.id == ChannelPost.item_id)
        .where(
            ChannelPost.status == ChannelPostStatus.PUBLISHED,
            ChannelPost.posted_at < now - timedelta(hours=refresh_h),
        )
    ).all()
    if stale_visible:
        issues["refresh_suggested"] = [{"post_id": pid, "item_id": c} for pid, c in stale_visible]
    should_promote = [s["item_id"] for s in suggestion_report(session, 10, now)["items"] if s["featured"]]
    if should_promote:
        issues["featured_ready_to_post"] = should_promote

    affected = sum(fixed.values())
    pending = session.execute(
        select(func.count()).select_from(ChannelPost).where(ChannelPost.status == ChannelPostStatus.REMOVAL_PENDING)
    ).scalar_one()
    report = {
        "affected": affected,
        "flagged": fixed["closed_items_flagged"] + fixed["blocked_owner_flagged"],
        "removal_pending": pending,
        "fixed": fixed,
        "issues": issues,
        "checked_at": now.isoformat(),
    }
    if affected:
        audit(
            session,
            AuditAction.CHANNEL_RECONCILIATION,
            actor_type=ActorType.WORKER,
            details={"fixed": fixed, "removal_pending": pending},
        )
    return report
