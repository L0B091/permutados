"""Outbox dispatcher: delivers queued notifications through the WhatsApp client.

Delivery rules
* Free-form (session) messages only inside the 24h customer-service window, measured from the user's last
  inbound message (``users.last_inbound_at``) minus a safety margin.
* Outside the window: one paid fallback template (``WHATSAPP_FALLBACK_TEMPLATE``) carrying a PII-free summary,
  at most once per silence period / 24h. The full message waits as ``AWAITING_WINDOW`` and is delivered as soon
  as the user writes again (``reopen_window``). If Meta answers 131047 (window closed) synchronously, or later
  in a status webhook, the same fallback applies.
* Messages with ``expires_at`` in the past are never delivered (``EXPIRED``): e.g. "tenés 24h para aceptar"
  after the offer already expired.
* Retries: exponential backoff (``next_attempt_at``) up to ``OUTBOX_MAX_ATTEMPTS``; permanent errors -> FAILED.
* Each notification is handled in its own transaction holding a row lock (``FOR UPDATE SKIP LOCKED``) during
  the HTTP call, so concurrent dispatchers never send the same row. A crash after Meta accepted the message but
  before COMMIT can resend it once (at-least-once).
* Sensitive bodies (contact release) are decrypted in memory only to hand them to WhatsApp, never logged.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.db import session_scope
from app.models.conversation import ConversationSession, Message, Notification
from app.models.enums import MessageDirection, MessageType, NotificationStatus
from app.models.user import User
from app.services.users import pii
from app.services.whatsapp.client import WhatsAppClient, WhatsAppError

log = logging.getLogger(__name__)


@dataclass
class DispatchStats:
    sent: int = 0
    templated: int = 0
    awaiting: int = 0
    retried: int = 0
    failed: int = 0
    expired: int = 0

    @property
    def total(self) -> int:
        return self.sent + self.templated + self.awaiting + self.retried + self.failed + self.expired


def window_open(last_inbound_at: datetime | None, now: datetime) -> bool:
    if last_inbound_at is None:
        return False
    s = get_settings()
    closes = last_inbound_at + timedelta(hours=s.whatsapp_session_window_hours)
    return now < closes - timedelta(minutes=s.whatsapp_window_safety_minutes)


def may_send_template(user: User, now: datetime) -> bool:
    """At most one paid template per silence period, and never more than one per 24h."""
    last = user.last_template_at
    if last is None:
        return True
    if user.last_inbound_at is not None and last <= user.last_inbound_at:
        return True
    return now - last >= timedelta(hours=24)


def backoff(attempts: int) -> timedelta:
    base = min(3600.0, 30.0 * (2 ** max(0, attempts - 1)))
    return timedelta(seconds=base * (0.75 + random.random() / 2))  # noqa: S311


def reopen_window(session: Session, user: User, now: datetime | None = None) -> int:
    """Called for every inbound message: the window is open again, release AWAITING_WINDOW messages."""
    now = now or utcnow()
    user.last_inbound_at = now
    res = session.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.status == NotificationStatus.AWAITING_WINDOW)
        .values(
            status=NotificationStatus.PENDING,
            next_attempt_at=None,
            payload=Notification.payload.op("-")("window_closed"),
        )
    )
    return res.rowcount or 0


def _record_outbound(session: Session, n: Notification, provider_id: str) -> None:
    if n.kind == "REPLY":
        return  # replies were already stored as conversation messages when generated
    conv_id = session.execute(
        select(ConversationSession.id).where(
            ConversationSession.user_id == n.user_id, ConversationSession.active.is_(True)
        )
    ).scalar_one_or_none()
    session.add(
        Message(
            session_id=conv_id,
            user_id=n.user_id,
            direction=MessageDirection.OUTBOUND,
            message_type=MessageType.TEXT,
            provider_message_id=provider_id,
            body=n.body,  # placeholder for sensitive notifications
        )
    )


def _retry_or_fail(n: Notification, err: WhatsAppError, now: datetime, stats: DispatchStats) -> None:
    n.last_error = f"{err.code}:{err.meta_code or ''}"[:500]
    if err.retryable and n.attempts < get_settings().outbox_max_attempts:
        n.next_attempt_at = now + backoff(n.attempts)
        stats.retried += 1
    else:
        n.status = NotificationStatus.FAILED
        stats.failed += 1
        log.warning("notification %s failed permanently: %s", n.id, n.last_error)


def _template_or_wait(
    n: Notification, user: User, to: str, client: WhatsAppClient, now: datetime, stats: DispatchStats
) -> None:
    s = get_settings()
    name = n.template_name or s.whatsapp_fallback_template
    if name and may_send_template(user, now):
        try:
            mid = client.send_template(
                to,
                name,
                language=s.whatsapp_template_language,
                body_params=[n.summary or "tenés una novedad en Permutados"],
                idempotency_key=f"{n.idempotency_key}:tpl",
            )
        except WhatsAppError as e:
            if e.retryable and n.attempts < s.outbox_max_attempts:
                n.last_error = f"template:{e.code}:{e.meta_code or ''}"[:500]
                n.next_attempt_at = now + backoff(n.attempts)
                stats.retried += 1
                return
            n.last_error = f"template:{e.code}:{e.meta_code or ''}"[:500]
            log.warning("fallback template failed for notification %s: %s", n.id, n.last_error)
        else:
            user.last_template_at = now
            n.payload = {**(n.payload or {}), "template_message_id": mid}
            n.channel = "TEMPLATE"
            stats.templated += 1
    n.status = NotificationStatus.AWAITING_WINDOW
    n.next_attempt_at = None
    stats.awaiting += 1


def deliver(session: Session, n: Notification, client: WhatsAppClient, now: datetime, stats: DispatchStats) -> None:
    if n.expires_at is not None and n.expires_at <= now:
        n.status = NotificationStatus.EXPIRED
        stats.expired += 1
        return
    user = session.execute(select(User).where(User.id == n.user_id).with_for_update()).scalar_one()
    to = pii.reveal_whatsapp_for_delivery(user)
    n.attempts += 1
    force_template = bool((n.payload or {}).get("window_closed"))
    if not force_template and window_open(user.last_inbound_at, now):
        try:
            mid = client.send_text(to, pii.reveal_notification_body(n), idempotency_key=n.idempotency_key)
        except WhatsAppError as e:
            if not e.outside_window:
                _retry_or_fail(n, e, now, stats)
                return
        else:
            n.status = NotificationStatus.SENT
            n.provider_message_id = mid
            n.sent_at = now
            n.channel = "SESSION"
            n.next_attempt_at = None
            n.last_error = None
            _record_outbound(session, n, mid)
            stats.sent += 1
            return
    _template_or_wait(n, user, to, client, now, stats)


def dispatch_outbox(client: WhatsAppClient, *, now: datetime | None = None, limit: int | None = None) -> DispatchStats:
    """Deliver due notifications, one transaction per notification."""
    s = get_settings()
    limit = limit or s.outbox_batch_size
    stats = DispatchStats()
    seen: set[int] = set()
    for _ in range(limit):
        current = now or utcnow()
        with session_scope() as session:
            stmt = (
                select(Notification)
                .where(
                    Notification.status == NotificationStatus.PENDING,
                    or_(Notification.next_attempt_at.is_(None), Notification.next_attempt_at <= current),
                )
                .order_by(Notification.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if seen:
                stmt = stmt.where(Notification.id.notin_(seen))
            n = session.execute(stmt).scalar_one_or_none()
            if n is None:
                break
            seen.add(n.id)
            try:
                with session.begin_nested():
                    deliver(session, n, client, current, stats)
            except Exception as e:  # a bug must not block the queue: park the row with backoff
                log.exception("unexpected error delivering notification %s", n.id)
                n.attempts += 1
                n.last_error = f"internal:{type(e).__name__}"
                if n.attempts >= s.outbox_max_attempts:
                    n.status = NotificationStatus.FAILED
                    stats.failed += 1
                else:
                    n.next_attempt_at = current + backoff(n.attempts)
                    stats.retried += 1
    return stats


def apply_status_update(
    session: Session, provider_message_id: str, status: str, error_code: int | None, now: datetime | None = None
) -> bool:
    """WhatsApp status webhook (sent/delivered/read/failed) for a message we sent."""
    n = session.execute(
        select(Notification).where(Notification.provider_message_id == provider_message_id).with_for_update()
    ).scalar_one_or_none()
    if n is None:
        return False
    if status == "failed":
        if error_code == 131047:
            # Meta accepted the request but the window was closed: fall back to the template path.
            n.status = NotificationStatus.PENDING
            n.next_attempt_at = None
            n.payload = {**(n.payload or {}), "window_closed": True}
        else:
            n.status = NotificationStatus.FAILED
        n.last_error = f"status_failed:{error_code or ''}"
    else:
        n.payload = {**(n.payload or {}), f"{status}_at": (now or utcnow()).isoformat()}
    return True
