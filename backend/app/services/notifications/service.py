"""Outbox for outbound WhatsApp messages. Delivery happens in the worker (``outbox.dispatch_outbox``) with
retries; ``idempotency_key`` guarantees a retried business operation never queues the same message twice."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.conversation import Notification
from app.services.notifications import templates
from app.services.users.pii import seal_notification_body

SENSITIVE_PLACEHOLDER = "[mensaje cifrado: contiene datos de contacto]"


def enqueue(
    session: Session,
    *,
    user_id: int,
    kind: str,
    body: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    template_name: str | None = None,
    sensitive: bool = False,
    expires_at: datetime | None = None,
    summary: str | None = None,
) -> Notification:
    """Queue a message. ``sensitive=True`` stores the rendered text only encrypted (contact release).
    ``expires_at``: the message is dropped (EXPIRED) if it could not be delivered before that moment."""
    values: dict[str, Any] = {
        "user_id": user_id,
        "kind": kind,
        "body": SENSITIVE_PLACEHOLDER if sensitive else body,
        "body_enc": seal_notification_body(body) if sensitive else None,
        "sensitive": sensitive,
        "summary": (summary or templates.summary_for(kind))[:200],
        "idempotency_key": idempotency_key,
        "payload": payload or {},
        "template_name": template_name,
        "expires_at": expires_at,
    }
    session.execute(insert(Notification).values(**values).on_conflict_do_nothing(index_elements=["idempotency_key"]))
    session.flush()
    return session.execute(select(Notification).where(Notification.idempotency_key == idempotency_key)).scalar_one()
