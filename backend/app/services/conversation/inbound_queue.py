"""Durable inbound queue: WhatsApp webhook -> webhook_events (RECEIVED) -> worker -> conversation engine.

* The webhook only verifies the signature, deduplicates by WhatsApp message id and stores an *encrypted*
  envelope (text, profile name) plus the encrypted identity (blind index + ciphertext of the number). It answers
  200 immediately, as Meta requires.
* The worker (``process_inbound_queue``) handles events strictly in arrival order under a session-level advisory
  lock (one consumer at a time, so per-user order is preserved), downloads media, runs ``handle_inbound`` and
  marks the event PROCESSED. The envelope and identity are scrubbed from the row afterwards.
* Failures: media download errors are retried with backoff (``next_attempt_at``) up to INBOUND_MAX_ATTEMPTS, then
  the user is asked to resend the photo. Unexpected errors roll the attempt back and retry later; a poisoned
  event ends as FAILED after the maximum attempts and never blocks the queue.
* Read receipts (mark-as-read) are sent best-effort after the commit.
"""

from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.db import get_engine, session_scope
from app.models.payment import WebhookEvent
from app.services.conversation.service import handle_inbound
from app.services.dolphin.client import DolphinClient
from app.services.mercadopago.gateway import PaymentGateway
from app.services.notifications.outbox import backoff
from app.services.publications.storage import ImageStorage
from app.services.users.pii import WaIdentity, identity_from_wa_id, open_inbound_envelope, seal_inbound_envelope
from app.services.whatsapp.client import WhatsAppClient, WhatsAppError
from app.services.whatsapp.parser import InboundMessage

log = logging.getLogger(__name__)
PROVIDER = "whatsapp"
_LOCK_KEY = zlib.crc32(b"permutados:inbound_queue")


def queue_payload(msg: InboundMessage) -> dict[str, Any]:
    """What the webhook stores. No plaintext phone number or message text."""
    return {
        "message_id": msg.message_id,
        "type": msg.type,
        "ts": msg.timestamp,
        "media_ids": list(msg.media_ids),
        "mime": msg.media_mime,
        "identity": identity_from_wa_id(msg.wa_id).as_dict(),
        "env": seal_inbound_envelope({"text": msg.text, "profile_name": msg.profile_name}),
    }


@dataclass
class InboundStats:
    processed: int = 0
    retried: int = 0
    failed: int = 0


@dataclass
class _Outcome:
    status: str  # PROCESSED | RETRY | FAILED
    mark_read: str | None = None
    error: str | None = None


def _process(
    session: Session,
    ev: WebhookEvent,
    *,
    dolphin: DolphinClient,
    wa: WhatsAppClient | None,
    gateway: PaymentGateway | None,
    storage: ImageStorage | None,
    now: datetime,
) -> _Outcome:
    s = get_settings()
    p = ev.payload or {}
    identity = WaIdentity.from_dict(p["identity"])
    env = open_inbound_envelope(p["env"]) if p.get("env") else {}
    mtype = p.get("type") or "text"
    images: list[bytes] | None = None
    mime: str | None = None
    media_failed = False
    if mtype == "image":
        media_ids = p.get("media_ids") or []
        if not media_ids or wa is None:
            media_failed = True
        else:
            try:
                f = wa.download_media(media_ids[0], max_bytes=s.max_image_bytes)
                images, mime = [f.data], f.mime_type
            except WhatsAppError as e:
                if e.retryable and ev.attempts + 1 < s.inbound_max_attempts:
                    return _Outcome("RETRY", error=f"media:{e.code}")
                media_failed = True
    handle_inbound(
        session,
        identity=identity,
        text=env.get("text"),
        images=images,
        image_mime=mime,
        message_type=mtype,
        provider_message_id=p.get("message_id"),
        profile_name=env.get("profile_name"),
        media_failed=media_failed,
        client=dolphin,
        gateway=gateway,
        storage=storage,
    )
    return _Outcome("PROCESSED", mark_read=p.get("message_id"))


def _scrubbed(payload: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    return {k: v for k, v in (payload or {}).items() if k not in ("env", "identity")} | extra


def process_inbound_queue(
    *,
    dolphin: DolphinClient,
    wa: WhatsAppClient | None,
    gateway: PaymentGateway | None = None,
    storage: ImageStorage | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> InboundStats:
    s = get_settings()
    limit = limit or s.inbound_batch_size
    stats = InboundStats()
    with get_engine().connect() as lock_conn:
        if not lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar():
            lock_conn.rollback()
            return stats
        lock_conn.commit()
        try:
            seen: set[int] = set()
            for _ in range(limit):
                current = now or utcnow()
                if not _one(dolphin, wa, gateway, storage, current, seen, stats):
                    break
        finally:
            lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            lock_conn.commit()
    return stats


def _one(dolphin, wa, gateway, storage, now: datetime, seen: set[int], stats: InboundStats) -> bool:
    s = get_settings()
    with session_scope() as session:
        stmt = (
            select(WebhookEvent)
            .where(
                WebhookEvent.provider == PROVIDER,
                WebhookEvent.status == "RECEIVED",
                or_(WebhookEvent.next_attempt_at.is_(None), WebhookEvent.next_attempt_at <= now),
            )
            .order_by(WebhookEvent.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if seen:
            stmt = stmt.where(WebhookEvent.id.notin_(seen))
        ev = session.execute(stmt).scalar_one_or_none()
        if ev is None:
            return False
        ev_id = ev.id
        seen.add(ev_id)
        try:
            out = _process(session, ev, dolphin=dolphin, wa=wa, gateway=gateway, storage=storage, now=now)
        except Exception as e:
            log.exception("inbound event %s failed", ev_id)
            session.rollback()
            out = _Outcome("ERROR", error=f"internal:{type(e).__name__}")
        ev = session.get(WebhookEvent, ev_id, with_for_update=True)
        assert ev is not None
        if out.status == "PROCESSED":
            ev.status, ev.processed_at, ev.error = "PROCESSED", now, None
            ev.payload = _scrubbed(ev.payload)
            stats.processed += 1
        else:
            ev.attempts += 1
            ev.error = out.error
            if ev.attempts >= s.inbound_max_attempts:
                ev.status, ev.processed_at = "FAILED", now
                ev.payload = _scrubbed(ev.payload)
                stats.failed += 1
            else:
                ev.next_attempt_at = now + backoff(ev.attempts)
                stats.retried += 1
    if out.mark_read and wa is not None:
        try:
            wa.mark_read(out.mark_read)
        except WhatsAppError:
            log.info("mark_read failed for an inbound message (ignored)")
    return True
