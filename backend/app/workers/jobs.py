"""Periodic jobs. Each is idempotent, runs in its own transaction and is protected by a PostgreSQL advisory
lock so several worker replicas never run the same job concurrently. After a restart the scheduler simply
runs them again: every job derives its work from PostgreSQL state (recover -> reconcile -> continue)."""

from __future__ import annotations

import logging
import zlib
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.db import session_scope
from app.models.enums import ActorType, ItemStatus
from app.models.item import Item
from app.services.audit.service import audit
from app.services.channel import service as channel
from app.services.notifications import templates
from app.services.notifications.service import enqueue
from app.services.offers import service as offers
from app.services.payments import service as payments
from app.services.publications.state import transition_item
from app.services.system import config_service as cfg
from app.services.system.modes import NOT_EMERGENCY, ONLINE_ONLY
from app.workers.runtime import Job, prune_history

log = logging.getLogger(__name__)


def warn_expiring_publications(session: Session, now=None) -> int:
    now = now or utcnow()
    days = int(cfg.get_value(session, "publication_expiry_warning_days"))
    if days <= 0:
        return 0
    items = (
        session.execute(
            select(Item)
            .where(
                Item.status == ItemStatus.PUBLISHED,
                Item.expires_at <= now + timedelta(days=days),
                Item.expires_at > now,
                Item.expiry_warning_sent_at.is_(None),
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for it in items:
        it.expiry_warning_sent_at = now
        enqueue(
            session,
            user_id=it.owner_id,
            kind="PUBLICATION_EXPIRY_WARNING",
            body=templates.publication_expiry_warning(
                item_code=it.public_id, title=it.title or "", expires_at=it.expires_at
            ),
            idempotency_key=f"item:{it.public_id}:expiry_warning",
            expires_at=it.expires_at,
        )
    return len(items)


def expire_publications(session: Session, now=None) -> int:
    """PUBLISHED items past expires_at -> EXPIRED (emits ITEM_EXPIRED -> channel removal flag).
    RESERVED items are left until their offer resolves; they expire on a later run."""
    now = now or utcnow()
    items = (
        session.execute(
            select(Item)
            .where(Item.status == ItemStatus.PUBLISHED, Item.expires_at <= now)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for it in items:
        transition_item(session, it, ItemStatus.EXPIRED, actor_type=ActorType.WORKER, reason="30_DAYS")
        enqueue(
            session,
            user_id=it.owner_id,
            kind="PUBLICATION_EXPIRED",
            body=templates.publication_expired(item_code=it.public_id, title=it.title or ""),
            idempotency_key=f"item:{it.public_id}:expired",
        )
    return len(items)


def run_with_lock(name: str, fn: Callable[[Session], int]) -> int | None:
    """Run ``fn`` once under the job's advisory lock (no mode policy / bookkeeping). Kept for scripts/tests."""
    key = zlib.crc32(f"permutados:{name}".encode())
    with session_scope() as session:
        if not session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": key}).scalar():
            return None
        n = fn(session)
        if n:
            audit(session, "WORKER_JOB", actor_type=ActorType.WORKER, details={"job": name, "affected": n})
        return n


def retry_event_handlers(session: Session) -> int:
    from app.services.events.bus import get_bus

    return get_bus().retry_failed(session)


def maintenance_jobs(gateway=None) -> list[Job]:
    """Periodic jobs with their system-mode policy (section 46):

    * deadline jobs (offer / payment window expiry) and contact releases run only ONLINE: while PAUSED or in
      EMERGENCY users cannot accept or pay, so deadlines are frozen and extended on resume (system.modes);
    * payment reconciliation, publication expiry, channel removal flags/reconciliation, event-handler retries,
      outbox recovery and pruning are safe and keep running in every mode;
    * score refresh is skipped in EMERGENCY.
    """
    from app.services.notifications.outbox import recover_stale_sending

    jobs = [
        Job("expire_pending_offers", offers.expire_pending_offers, 60, ONLINE_ONLY, description="offer 24h expiry"),
        Job(
            "expire_payment_windows",
            lambda s: offers.expire_payment_windows(s, gateway=gateway),
            60,
            ONLINE_ONLY,
            description="payment 24h window expiry (asks Mercado Pago first)",
        ),
        Job("release_deferred", payments.release_deferred, 60, ONLINE_ONLY, description="releases deferred by mode"),
        Job("warn_expiring_publications", warn_expiring_publications, 3600, description="30-day expiry warning"),
        Job("expire_publications", expire_publications, 300, description="30-day expiry"),
        Job("channel_reconciliation", channel.reconcile, 600, description="channel DB vs recorded state"),
        Job("refresh_channel_scores", channel.refresh_scores, 900, NOT_EMERGENCY, description="attractiveness"),
        Job("retry_event_handlers", retry_event_handlers, 60, description="failed best-effort event handlers"),
        Job(
            "recover_outbox_in_flight",
            lambda: recover_stale_sending(),
            300,
            transactional=False,
            description="notifications left in SENDING by a crash",
        ),
        Job("prune_history", prune_history, 86400, description="retention of bookkeeping tables"),
    ]
    if gateway is not None:
        jobs.append(
            Job(
                "reconcile_pending_payments",
                lambda s: payments.reconcile_pending_payments(s, gateway),
                300,
                description="payments approved while webhooks were lost",
            )
        )
    return jobs


def messaging_jobs(*, dolphin, wa, gateway=None, storage=None) -> dict[str, tuple[Callable[[], int], int]]:
    """Jobs that manage their own transactions (one per message): inbound queue and outbox."""
    from app.core.config import get_settings
    from app.services.conversation.inbound_queue import process_inbound_queue
    from app.services.notifications.outbox import dispatch_outbox

    s = get_settings()
    jobs: dict[str, tuple[Callable[[], int], int]] = {
        "process_inbound_queue": (
            lambda: process_inbound_queue(dolphin=dolphin, wa=wa, gateway=gateway, storage=storage).processed,
            s.inbound_poll_seconds,
        ),
    }
    if wa is not None:
        jobs["dispatch_outbox"] = (lambda: dispatch_outbox(wa).total, s.outbox_poll_seconds)
    else:
        log.warning("WhatsApp is not configured: outbound messages stay queued in the outbox")
    return jobs


def messaging_job_objects(*, dolphin, wa, gateway=None, storage=None) -> list[Job]:
    return [
        Job(name, fn, interval, transactional=False, record_idle=False)
        for name, (fn, interval) in messaging_jobs(dolphin=dolphin, wa=wa, gateway=gateway, storage=storage).items()
    ]


def job_definitions(gateway=None) -> dict[str, tuple[Callable[[Session], int], int]]:
    """name -> (function, interval seconds) for the transactional maintenance jobs (compat helper)."""
    return {j.name: (j.fn, j.interval) for j in maintenance_jobs(gateway) if j.transactional}
