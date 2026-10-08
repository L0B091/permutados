"""Outbox for outbound WhatsApp messages. Delivery happens in a worker (phase 2) with retries;
``idempotency_key`` guarantees a retried operation never sends the same message twice."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.conversation import Notification


def enqueue(
    session: Session,
    *,
    user_id: int,
    kind: str,
    body: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    template_name: str | None = None,
) -> Notification:
    stmt = (
        insert(Notification)
        .values(
            user_id=user_id,
            kind=kind,
            body=body,
            idempotency_key=idempotency_key,
            payload=payload or {},
            template_name=template_name,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    session.execute(stmt)
    session.flush()
    return session.execute(select(Notification).where(Notification.idempotency_key == idempotency_key)).scalar_one()
