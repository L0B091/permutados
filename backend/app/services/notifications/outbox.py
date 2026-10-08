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
* No duplicates on crash (spec 47 "reintentar sin duplicar"): a notification is first *claimed* in its own
  committed transaction (``FOR UPDATE SKIP LOCKED`` -> status ``SENDING``), then handed to WhatsApp outside any
  transaction, then settled in a second transaction. Every send carries ``biz_opaque_callback_data`` = the
  idempotency key, and Meta echoes it in the status webhooks. If the worker dies between the HTTP call and the
  settle step, the row stays ``SENDING``; when the "sent" status webhook arrives it is matched by that key and
  the row is marked SENT (``apply_status_update``). Only if no status arrives within
  ``OUTBOX_SENDING_GRACE_SECONDS`` does ``recover_stale_sending`` put a session message back to PENDING (Meta
  never accepted it). An in-doubt *template* is never resent (it costs money): the message waits for the user.
* EMERGENCY mode holds sensitive (contact-release) messages until the mode is lifted.
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
from app.models.enums import MessageDirection, MessageType, NotificationStatus, SystemMode
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
    held: int = 0

    @property
    def total(self) -> int:
        return self.sent + self.templated + self.awaiting + self.retried + self.failed + self.expired + self.held


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


HOLD_SENSITIVE_IN_EMERGENCY = timedelta(minutes=5)


@dataclass
class _Claim:
    nid: int
    mode: str  # SESSION | TEMPLATE
    to: str
    key: str
    started: datetime
    text: str | None = None
    template: str | None = None
    summary: str | None = None


def _claim(session: Session, n: Notification, now: datetime, stats: DispatchStats) -> _Claim | None:
    """Decide how to deliver ``n`` (locked, PENDING, due). Returns a claim to send, or None when settled here."""
    if n.expires_at is not None and n.expires_at <= now:
        n.status = NotificationStatus.EXPIRED
        stats.expired += 1
        return None
    if n.sensitive:
        from app.services.system import config_service as cfg

        if cfg.current_mode(session) == SystemMode.EMERGENCY:
            n.next_attempt_at = now + HOLD_SENSITIVE_IN_EMERGENCY
            stats.held += 1
            return None
    user = session.execute(select(User).where(User.id == n.user_id).with_for_update()).scalar_one()
    to = pii.reveal_whatsapp_for_delivery(user)
    sending: dict = {}
    if not (n.payload or {}).get("window_closed") and window_open(user.last_inbound_at, now):
        claim = _Claim(n.id, "SESSION", to, n.idempotency_key, now, text=pii.reveal_notification_body(n))
    else:
        s = get_settings()
        name = n.template_name or s.whatsapp_fallback_template
        if not (name and may_send_template(user, now)):
            n.status = NotificationStatus.AWAITING_WINDOW
            n.next_attempt_at = None
            stats.awaiting += 1
            return None
        # Reserve the template slot now so a concurrent dispatcher cannot send a second paid template.
        sending["prev_template_at"] = user.last_template_at.isoformat() if user.last_template_at else None
        user.last_template_at = now
        claim = _Claim(
            n.id,
            "TEMPLATE",
            to,
            f"{n.idempotency_key}:tpl",
            now,
            template=name,
            summary=n.summary or "tenés una novedad en Permutados",
        )
    n.attempts += 1
    n.status = NotificationStatus.SENDING
    n.sending_started_at = now
    n.payload = {**(n.payload or {}), "sending": {"mode": claim.mode, **sending}}
    return claim


def _send(client: WhatsAppClient, c: _Claim) -> str:
    if c.mode == "SESSION":
        return client.send_text(c.to, c.text or "", idempotency_key=c.key)
    s = get_settings()
    return client.send_template(
        c.to,
        c.template or "",
        language=s.whatsapp_template_language,
        body_params=[c.summary or ""],
        idempotency_key=c.key,
    )


def _mark_sent(session: Session, n: Notification, mid: str, now: datetime) -> None:
    n.status = NotificationStatus.SENT
    n.provider_message_id = mid
    n.sent_at = now
    n.channel = "SESSION"
    n.next_attempt_at = None
    n.last_error = None
    n.sending_started_at = None
    _record_outbound(session, n, mid)


def _mark_template_sent(n: Notification, mid: str) -> None:
    n.payload = {**(n.payload or {}), "template_message_id": mid}
    n.channel = "TEMPLATE"
    n.status = NotificationStatus.AWAITING_WINDOW
    n.next_attempt_at = None
    n.sending_started_at = None


def _settle(
    session: Session, c: _Claim, mid: str | None, err: WhatsAppError | None, now: datetime, stats: DispatchStats
) -> bool:
    """Record the outcome of a send. Returns True when the row must be processed again right away."""
    n = session.execute(select(Notification).where(Notification.id == c.nid).with_for_update()).scalar_one()
    if n.status != NotificationStatus.SENDING or n.sending_started_at != c.started:
        # Already settled by the status webhook (matched through biz_opaque_callback_data) or by recovery.
        if mid and c.mode == "SESSION" and n.provider_message_id is None:
            n.provider_message_id = mid
        stats.sent += 1 if c.mode == "SESSION" and err is None else 0
        return False
    payload = dict(n.payload or {})
    sending = payload.pop("sending", {}) or {}
    n.payload = payload
    n.sending_started_at = None
    if c.mode == "SESSION":
        if err is None:
            _mark_sent(session, n, mid or "", now)
            stats.sent += 1
            return False
        n.status = NotificationStatus.PENDING
        if err.outside_window:
            n.payload = {**payload, "window_closed": True}
            n.next_attempt_at = None
            return True
        _retry_or_fail(n, err, now, stats)
        return False
    user = session.execute(select(User).where(User.id == n.user_id).with_for_update()).scalar_one()
    if err is None:
        _mark_template_sent(n, mid or "")
        stats.templated += 1
        stats.awaiting += 1
        return False
    prev = sending.get("prev_template_at")
    user.last_template_at = datetime.fromisoformat(prev) if prev else None
    n.last_error = f"template:{err.code}:{err.meta_code or ''}"[:500]
    if err.retryable and n.attempts < get_settings().outbox_max_attempts:
        n.status = NotificationStatus.PENDING
        n.next_attempt_at = now + backoff(n.attempts)
        stats.retried += 1
        return False
    log.warning("fallback template failed for notification %s: %s", n.id, n.last_error)
    n.status = NotificationStatus.AWAITING_WINDOW
    n.next_attempt_at = None
    stats.awaiting += 1
    return False


def _park(n: Notification, e: Exception, now: datetime, stats: DispatchStats) -> None:
    """A bug must not block the queue: park the row with backoff."""
    log.exception("unexpected error delivering notification %s", n.id)
    n.status = NotificationStatus.PENDING
    n.sending_started_at = None
    n.attempts += 1
    n.last_error = f"internal:{type(e).__name__}"
    if n.attempts >= get_settings().outbox_max_attempts:
        n.status = NotificationStatus.FAILED
        stats.failed += 1
    else:
        n.next_attempt_at = now + backoff(n.attempts)
        stats.retried += 1


def _claim_next(
    current: datetime, seen: set[int], stats: DispatchStats, only_id: int | None
) -> tuple[bool, _Claim | None]:
    """Transaction A. Returns (found_a_row, claim)."""
    with session_scope() as session:
        stmt = (
            select(Notification)
            .where(Notification.status == NotificationStatus.PENDING)
            .order_by(Notification.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if only_id is not None:
            stmt = stmt.where(Notification.id == only_id)
        else:
            stmt = stmt.where(or_(Notification.next_attempt_at.is_(None), Notification.next_attempt_at <= current))
            if seen:
                stmt = stmt.where(Notification.id.notin_(seen))
        n = session.execute(stmt).scalar_one_or_none()
        if n is None:
            return False, None
        seen.add(n.id)
        try:
            with session.begin_nested():
                return True, _claim(session, n, current, stats)
        except Exception as e:
            _park(n, e, current, stats)
            return True, None


def dispatch_outbox(client: WhatsAppClient, *, now: datetime | None = None, limit: int | None = None) -> DispatchStats:
    """Deliver due notifications: claim (tx) -> send (no tx) -> settle (tx), one notification at a time."""
    s = get_settings()
    limit = limit or s.outbox_batch_size
    stats = DispatchStats()
    seen: set[int] = set()
    again: int | None = None
    for _ in range(limit * 2):
        if again is None and len(seen) >= limit:
            break
        current = now or utcnow()
        retry_id, again = again, None
        found, claim = _claim_next(current, seen, stats, retry_id)
        if not found:
            if retry_id is not None:
                continue
            break
        if claim is None:
            continue
        mid, err = None, None
        try:
            mid = _send(client, claim)
        except WhatsAppError as e:
            err = e
        except Exception as e:  # unexpected client bug: retryable
            log.exception("unexpected WhatsApp client error for notification %s", claim.nid)
            err = WhatsAppError(f"internal:{type(e).__name__}", code="INTERNAL", retryable=True)
        with session_scope() as session:
            if _settle(session, claim, mid, err, current, stats):
                again = claim.nid
    return stats


def recover_stale_sending(*, now: datetime | None = None, grace_seconds: int | None = None) -> int:
    """Crash recovery for rows left in SENDING (worker died between the WhatsApp call and the settle step).

    Called at worker start-up and periodically. Rows already confirmed by a status webhook are no longer SENDING.
    Session messages with no evidence after the grace period go back to PENDING (Meta never accepted them);
    templates in doubt are NOT resent (paid, and the user may already have it): the full message waits.
    """
    now = now or utcnow()
    grace = timedelta(
        seconds=grace_seconds if grace_seconds is not None else get_settings().outbox_sending_grace_seconds
    )
    n_rec = 0
    with session_scope() as session:
        rows = (
            session.execute(
                select(Notification)
                .where(Notification.status == NotificationStatus.SENDING, Notification.sending_started_at < now - grace)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for n in rows:
            payload = dict(n.payload or {})
            mode = (payload.pop("sending", {}) or {}).get("mode")
            payload["recovered"] = int(payload.get("recovered", 0)) + 1
            n.payload = payload
            n.sending_started_at = None
            n.last_error = "recovered:unknown_outcome"
            if mode == "TEMPLATE":
                n.status = NotificationStatus.AWAITING_WINDOW
                n.next_attempt_at = None
            else:
                n.status = NotificationStatus.PENDING
                n.next_attempt_at = None
            n_rec += 1
            log.warning("recovered in-flight notification %s (mode %s) after a crash", n.id, mode)
    return n_rec


def apply_status_update(
    session: Session,
    provider_message_id: str,
    status: str,
    error_code: int | None,
    now: datetime | None = None,
    *,
    callback_data: str | None = None,
) -> bool:
    """WhatsApp status webhook (sent/delivered/read/failed) for a message we sent.

    Matched by the wamid or, for a send whose outcome we never recorded (crash), by ``biz_opaque_callback_data``
    which carries our idempotency key (``<key>`` for session messages, ``<key>:tpl`` for templates)."""
    now = now or utcnow()
    n = session.execute(
        select(Notification).where(Notification.provider_message_id == provider_message_id).with_for_update()
    ).scalar_one_or_none()
    if n is None and callback_data:
        is_tpl = callback_data.endswith(":tpl")
        key = callback_data[:-4] if is_tpl else callback_data
        n = session.execute(
            select(Notification).where(Notification.idempotency_key == key).with_for_update()
        ).scalar_one_or_none()
        if n is None:
            return False
        if n.status == NotificationStatus.SENDING and status != "failed":
            if is_tpl:
                _mark_template_sent(n, provider_message_id)
            else:
                _mark_sent(session, n, provider_message_id, now)
            n.payload = {k: v for k, v in (n.payload or {}).items() if k != "sending"}
            log.info("notification %s confirmed by status webhook (in-flight recovery)", n.id)
        elif is_tpl:
            if (n.payload or {}).get("template_message_id") not in (None, provider_message_id):
                log.warning("duplicate template detected for notification %s", n.id)
            return True
        elif n.provider_message_id and n.provider_message_id != provider_message_id:
            dups = list((n.payload or {}).get("duplicate_message_ids", []))
            n.payload = {**(n.payload or {}), "duplicate_message_ids": [*dups, provider_message_id][-5:]}
            log.warning("duplicate WhatsApp delivery detected for notification %s", n.id)
            return True
    if n is None:
        return False
    if status == "failed":
        if n.status == NotificationStatus.SENDING:
            n.sending_started_at = None
            n.payload = {k: v for k, v in (n.payload or {}).items() if k != "sending"}
        if error_code == 131047:
            # Meta accepted the request but the window was closed: fall back to the template path.
            n.status = NotificationStatus.PENDING
            n.next_attempt_at = None
            n.payload = {**(n.payload or {}), "window_closed": True}
        else:
            n.status = NotificationStatus.FAILED
        n.last_error = f"status_failed:{error_code or ''}"
    else:
        n.payload = {**(n.payload or {}), f"{status}_at": now.isoformat()}
    return True
