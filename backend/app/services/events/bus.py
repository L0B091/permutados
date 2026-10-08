"""Internal event bus (section 35).

Events are persisted in ``domain_events`` and dispatched synchronously to subscribers inside
the same DB transaction, so e.g. flagging a channel post for removal is atomic with the item
becoming COMPLETED. Periodic reconciliation covers anything that could be missed.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.audit import DomainEvent

log = logging.getLogger(__name__)


class EventType(StrEnum):
    ITEM_CREATED = "ITEM_CREATED"
    ITEM_UPDATED = "ITEM_UPDATED"
    ITEM_STATUS_CHANGED = "ITEM_STATUS_CHANGED"
    ITEM_COMPLETED = "ITEM_COMPLETED"
    ITEM_EXPIRED = "ITEM_EXPIRED"
    ITEM_DELETED = "ITEM_DELETED"
    OFFER_CREATED = "OFFER_CREATED"
    OFFER_ACCEPTED = "OFFER_ACCEPTED"
    OFFER_CLOSED = "OFFER_CLOSED"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    CONTACT_RELEASED = "CONTACT_RELEASED"
    USER_SUSPENDED = "USER_SUSPENDED"


Handler = Callable[[Session, DomainEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: EventType | str, handler: Handler) -> None:
        if handler not in self._handlers[str(event_type)]:
            self._handlers[str(event_type)].append(handler)

    def publish(
        self,
        session: Session,
        event_type: EventType | str,
        *,
        aggregate_type: str,
        aggregate_id: str | int,
        payload: dict[str, Any] | None = None,
    ) -> DomainEvent:
        event = DomainEvent(
            event_type=str(event_type),
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            payload=payload or {},
        )
        session.add(event)
        session.flush()
        for handler in self._handlers.get(str(event_type), []):
            # Handlers run in the caller's transaction: if one fails, the whole operation rolls back.
            handler(session, event)
        event.processed_at = utcnow()
        return event


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
        from app.services.channel.service import register_event_handlers

        register_event_handlers(_bus)
    return _bus
