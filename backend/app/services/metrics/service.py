"""Dashboard metrics for the admin console (section 48). Read-only aggregate queries; no personal data.

``period`` metrics cover the last ``days`` days; ``current`` metrics are a snapshot. All money is in the
configured currency (ARS); revenue counts only provider-verified APPROVED payments.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Float, cast, func, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.audit import AuditLog
from app.models.channel import ChannelExposure, ChannelPost
from app.models.conversation import Message, Notification, SearchSession
from app.models.enums import (
    BanStatus,
    ChannelPostStatus,
    ItemStatus,
    MessageDirection,
    NotificationStatus,
    OfferStatus,
    PaymentStatus,
    SecurityEventType,
    TransactionStatus,
    UserStatus,
)
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.payment import Payment, WebhookEvent
from app.models.security import Ban, SecurityEvent
from app.models.user import User
from app.models.worker import JobRun, JobState
from app.services.system import config_service as cfg
from app.workers.runtime import alive_workers

ACCEPTED = (OfferStatus.ACCEPTED, OfferStatus.PAYMENT_PENDING, OfferStatus.PAID, OfferStatus.CONTACT_RELEASED)


def _count(session: Session, model, *where) -> int:
    return session.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _percentiles(session: Session, expr, *where) -> dict[str, float | None]:
    row = session.execute(
        select(
            func.percentile_cont(0.5).within_group(expr),
            func.percentile_cont(0.95).within_group(expr),
            func.avg(expr),
            func.count(),
        ).where(*where)
    ).one()
    p50, p95, avg, n = row
    return {
        "p50_seconds": round(float(p50), 2) if p50 is not None else None,
        "p95_seconds": round(float(p95), 2) if p95 is not None else None,
        "avg_seconds": round(float(avg), 2) if avg is not None else None,
        "samples": n,
    }


def _seconds(a, b):
    return cast(func.extract("epoch", a - b), Float)


def dashboard_metrics(session: Session, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    since = now - timedelta(days=days)

    # ---- users
    users = {
        "total": _count(session, User),
        "new": _count(session, User, User.created_at >= since),
        "active": _count(session, User, User.last_inbound_at >= since),
        "suspended": _count(session, User, User.status == UserStatus.SUSPENDED),
        "banned": _count(session, User, User.status == UserStatus.BANNED),
    }

    # ---- publications
    by_status = dict(session.execute(select(Item.status, func.count()).group_by(Item.status)).all())
    publications = {
        "by_status": {s.value: by_status.get(s, 0) for s in ItemStatus},
        "published_in_period": _count(session, Item, Item.published_at >= since),
        "completed_in_period": _count(session, Item, Item.completed_at >= since),
        "expired_in_period": _count(
            session,
            AuditLog,
            AuditLog.action == "ITEM_EXPIRED",
            AuditLog.created_at >= since,
        ),
        "deleted_in_period": _count(session, Item, Item.deleted_at >= since),
        "avg_score_published": session.execute(
            select(func.avg(Item.attractiveness_score)).where(Item.status == ItemStatus.PUBLISHED)
        ).scalar_one(),
    }
    if publications["avg_score_published"] is not None:
        publications["avg_score_published"] = round(float(publications["avg_score_published"]), 1)

    # ---- searches
    searches = {
        "sessions": _count(session, SearchSession, SearchSession.created_at >= since),
        "distinct_searchers": session.execute(
            select(func.count(func.distinct(SearchSession.user_id))).where(SearchSession.created_at >= since)
        ).scalar_one(),
        "zero_result_sessions": _count(
            session,
            SearchSession,
            SearchSession.created_at >= since,
            func.cardinality(SearchSession.shown_item_ids) == 0,
        ),
    }

    # ---- offers (created in the period, by current outcome)
    offer_rows = dict(
        session.execute(
            select(Offer.status, func.count()).where(Offer.created_at >= since).group_by(Offer.status)
        ).all()
    )
    created = sum(offer_rows.values())
    accepted = sum(offer_rows.get(s, 0) for s in ACCEPTED)
    rejected = offer_rows.get(OfferStatus.REJECTED, 0)
    expired = offer_rows.get(OfferStatus.EXPIRED, 0)
    offers = {
        "created": created,
        "by_status": {s.value: offer_rows.get(s, 0) for s in OfferStatus},
        "accepted": accepted,
        "rejected": rejected,
        "expired": expired,
        "cancelled": offer_rows.get(OfferStatus.CANCELLED, 0),
        "pending_now": _count(session, Offer, Offer.status == OfferStatus.PENDING),
        "acceptance_rate": _rate(accepted, accepted + rejected + expired),
        "rejection_rate": _rate(rejected, accepted + rejected + expired),
        "expiration_rate": _rate(expired, created),
    }

    # ---- payments / revenue
    pay_rows = dict(
        session.execute(
            select(Payment.status, func.count()).where(Payment.created_at >= since).group_by(Payment.status)
        ).all()
    )
    revenue = session.execute(
        select(Payment.currency, func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.status == PaymentStatus.APPROVED, Payment.verified_at >= since)
        .group_by(Payment.currency)
    ).all()
    revenue_total = session.execute(
        select(Payment.currency, func.coalesce(func.sum(Payment.amount), 0))
        .where(Payment.status == PaymentStatus.APPROVED)
        .group_by(Payment.currency)
    ).all()
    payments = {
        "checkouts_created": sum(pay_rows.values()),
        "by_status": {s.value: pay_rows.get(s, 0) for s in PaymentStatus},
        "approved": _count(session, Payment, Payment.status == PaymentStatus.APPROVED, Payment.verified_at >= since),
        "needs_refund_now": _count(session, Payment, Payment.status == PaymentStatus.NEEDS_REFUND),
        "mismatches": _count(
            session,
            SecurityEvent,
            SecurityEvent.event_type == SecurityEventType.PAYMENT_MISMATCH,
            SecurityEvent.created_at >= since,
        ),
        "revenue": {cur: str(Decimal(v)) for cur, v in revenue},
        "revenue_all_time": {cur: str(Decimal(v)) for cur, v in revenue_total},
    }
    released = _count(session, Transaction, Transaction.released_at >= since)
    tx_created = _count(session, Transaction, Transaction.created_at >= since)
    contacts = {
        "released": released,
        "awaiting_payment_now": _count(session, Transaction, Transaction.status == TransactionStatus.PAYMENT_PENDING),
        "paid_awaiting_release_now": _count(session, Transaction, Transaction.status == TransactionStatus.PAID),
        "payment_timeouts": _count(
            session, Transaction, Transaction.status == TransactionStatus.EXPIRED, Transaction.closed_at >= since
        ),
    }
    conversion = {
        "search_to_offer": _rate(created, searches["sessions"]),
        "offer_to_acceptance": _rate(accepted, created),
        "acceptance_to_payment": _rate(released, tx_created),
        "offer_to_release": _rate(released, created),
        "published_to_completed": _rate(publications["completed_in_period"], publications["published_in_period"]),
    }

    # ---- channel
    post_rows = dict(session.execute(select(ChannelPost.status, func.count()).group_by(ChannelPost.status)).all())
    channel = {
        "posts_by_status": {s.value: post_rows.get(s, 0) for s in ChannelPostStatus},
        "posted_in_period": _count(session, ChannelPost, ChannelPost.posted_at >= since),
        "exposures_in_period": _count(session, ChannelExposure, ChannelExposure.exposed_at >= since),
        "avg_score_at_post": session.execute(
            select(func.avg(ChannelExposure.score)).where(ChannelExposure.exposed_at >= since)
        ).scalar_one(),
        "removal_pending_now": post_rows.get(ChannelPostStatus.REMOVAL_PENDING, 0),
        "featured_items": _count(session, Item, Item.featured.is_(True), Item.status == ItemStatus.PUBLISHED),
    }
    if channel["avg_score_at_post"] is not None:
        channel["avg_score_at_post"] = round(float(channel["avg_score_at_post"]), 1)

    # ---- errors / security
    sec_rows = dict(
        session.execute(
            select(SecurityEvent.event_type, func.count())
            .where(SecurityEvent.created_at >= since)
            .group_by(SecurityEvent.event_type)
        ).all()
    )
    errors = {
        "notifications_failed": _count(
            session, Notification, Notification.status == NotificationStatus.FAILED, Notification.updated_at >= since
        ),
        "notifications_pending_now": _count(session, Notification, Notification.status == NotificationStatus.PENDING),
        "notifications_awaiting_window_now": _count(
            session, Notification, Notification.status == NotificationStatus.AWAITING_WINDOW
        ),
        "inbound_failed": _count(
            session, WebhookEvent, WebhookEvent.status == "FAILED", WebhookEvent.received_at >= since
        ),
        "inbound_backlog_now": _count(
            session, WebhookEvent, WebhookEvent.provider == "whatsapp", WebhookEvent.status == "RECEIVED"
        ),
        "job_failures": _count(session, JobRun, JobRun.status == "FAILED", JobRun.started_at >= since),
        "jobs_abandoned": _count(session, JobRun, JobRun.status == "ABANDONED", JobRun.started_at >= since),
        "security_events": {k.value: v for k, v in sec_rows.items()},
    }
    bans = {
        "pending_review_now": _count(session, Ban, Ban.status == BanStatus.PENDING_REVIEW),
        "confirmed_in_period": _count(session, Ban, Ban.status == BanStatus.CONFIRMED, Ban.reviewed_at >= since),
        "lifted_in_period": _count(session, Ban, Ban.status == BanStatus.LIFTED, Ban.reviewed_at >= since),
        "auto_suspensions_in_period": _count(session, Ban, Ban.created_at >= since),
    }

    # ---- performance
    performance = {
        "outbox_delivery": _percentiles(
            session,
            _seconds(Notification.sent_at, Notification.created_at),
            Notification.sent_at.is_not(None),
            Notification.sent_at >= since,
        ),
        "inbound_processing": _percentiles(
            session,
            _seconds(WebhookEvent.processed_at, WebhookEvent.received_at),
            WebhookEvent.provider == "whatsapp",
            WebhookEvent.processed_at.is_not(None),
            WebhookEvent.received_at >= since,
        ),
        "bot_reply_latency": _bot_latency(session, since),
        "jobs": [
            {
                "job": st.job_name,
                "status": st.status.value,
                "last_success_at": st.last_success_at.isoformat() if st.last_success_at else None,
                "last_duration_ms": st.last_duration_ms,
                "consecutive_failures": st.consecutive_failures,
                "runs": st.runs,
                "failures": st.failures,
            }
            for st in session.execute(select(JobState).order_by(JobState.job_name)).scalars()
        ],
        "workers_alive": [
            {
                "worker_id": w.worker_id,
                "last_seen_at": w.last_seen_at.isoformat(),
                "started_at": w.started_at.isoformat(),
            }
            for w in alive_workers(session, now)
        ],
    }

    return {
        "generated_at": now.isoformat(),
        "period_days": days,
        "system_mode": cfg.current_mode(session).value,
        "users": users,
        "publications": publications,
        "searches": searches,
        "offers": offers,
        "payments": payments,
        "contacts": contacts,
        "conversion": conversion,
        "channel": channel,
        "errors": errors,
        "bans": bans,
        "performance": performance,
        "daily": daily_series(session, min(days, 90), now),
    }


def _bot_latency(session: Session, since: datetime) -> dict[str, float | None]:
    """Seconds between an inbound message and the next outbound message to the same user (approximation)."""
    inbound = Message.__table__.alias("i")
    outbound = Message.__table__.alias("o")
    nxt = (
        select(func.min(outbound.c.created_at))
        .where(
            outbound.c.user_id == inbound.c.user_id,
            outbound.c.direction == MessageDirection.OUTBOUND.value,
            outbound.c.created_at >= inbound.c.created_at,
        )
        .scalar_subquery()
    )
    sub = (
        select(_seconds(nxt, inbound.c.created_at).label("lat"))
        .where(inbound.c.direction == MessageDirection.INBOUND.value, inbound.c.created_at >= since)
        .subquery()
    )
    row = session.execute(
        select(
            func.percentile_cont(0.5).within_group(sub.c.lat),
            func.percentile_cont(0.95).within_group(sub.c.lat),
            func.count(sub.c.lat),
        )
    ).one()
    return {
        "p50_seconds": round(float(row[0]), 2) if row[0] is not None else None,
        "p95_seconds": round(float(row[1]), 2) if row[1] is not None else None,
        "samples": row[2],
    }


def daily_series(session: Session, days: int, now: datetime) -> list[dict[str, Any]]:
    """Per-day counts (Argentina time) for charts."""
    tz = "America/Argentina/Buenos_Aires"
    since = now - timedelta(days=days)

    def per_day(column, *where) -> dict[str, int]:
        day = func.date(func.timezone(tz, column))
        return {
            str(d): n
            for d, n in session.execute(select(day, func.count()).where(column >= since, *where).group_by(day)).all()
        }

    def money_per_day() -> dict[str, str]:
        day = func.date(func.timezone(tz, Payment.verified_at))
        return {
            str(d): str(v)
            for d, v in session.execute(
                select(day, func.sum(Payment.amount))
                .where(Payment.status == PaymentStatus.APPROVED, Payment.verified_at >= since)
                .group_by(day)
            ).all()
        }

    series = {
        "new_users": per_day(User.created_at),
        "publications": per_day(Item.published_at),
        "searches": per_day(SearchSession.created_at),
        "offers": per_day(Offer.created_at),
        "releases": per_day(Transaction.released_at),
    }
    revenue = money_per_day()
    out = []
    from app.services.notifications.templates import AR_TZ

    start = since.astimezone(AR_TZ).date()
    for i in range(days + 1):
        d = str(start + timedelta(days=i))
        out.append({"date": d, **{k: v.get(d, 0) for k, v in series.items()}, "revenue": revenue.get(d, "0")})
    return out
