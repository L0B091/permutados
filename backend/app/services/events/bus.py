"""Internal event bus (section 35).

Every event is persisted in ``domain_events`` in the caller's transaction and dispatched synchronously, so
reactions are immediate (no polling). Two kinds of subscribers:

* ``critical=True``: runs in the caller's transaction; if it raises, the whole operation rolls back.
* best effort (default): runs inside a SAVEPOINT. A failure is rolled back *only for that handler*, logged and
  recorded in ``domain_events.failed_handlers``; the operation that emitted the event still commits (e.g. a
  channel bug can never block a contact release). The worker job ``retry_event_handlers`` re-runs failed
  handlers with backoff; ``processed_at`` is set once every handler has succeeded.

Handlers must be idempotent (they may run again after a retry).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import select
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
    OFFER_REJECTED = "OFFER_REJECTED"
    OFFER_EXPIRED = "OFFER_EXPIRED"
    OFFER_CANCELLED = "OFFER_CANCELLED"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    CONTACT_RELEASED = "CONTACT_RELEASED"
    USER_SUSPENDED = "USER_SUSPENDED"
    USER_BANNED = "USER_BANNED"


Handler = Callable[[Session, DomainEvent], None]
MAX_HANDLER_ATTEMPTS = 8


def handler_name(handler: Handler) -> str:
    return f"{handler.__module__}.{getattr(handler, '__qualname__', repr(handler))}"


@dataclass(frozen=True)
class _Sub:
    handler: Handler
    critical: bool

    @property
    def name(self) -> str:
        return handler_name(self.handler)


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[_Sub]] = defaultdict(list)

    def subscribe(self, event_type: EventType | str, handler: Handler, *, critical: bool = False) -> None:
        subs = self._subs[str(event_type)]
        if all(s.handler is not handler for s in subs):
            subs.append(_Sub(handler, critical))

    def subscribers(self, event_type: EventType | str) -> list[str]:
        return [s.name for s in self._subs.get(str(event_type), [])]

    def _run(self, session: Session, event: DomainEvent, subs: list[_Sub]) -> list[str]:
        failed: list[str] = []
        for sub in subs:
            if sub.critical:
                sub.handler(session, event)  # propagates: the caller's transaction rolls back
                continue
            try:
                with session.begin_nested():
                    sub.handler(session, event)
            except Exception as e:
                log.exception("event handler %s failed for %s #%s", sub.name, event.event_type, event.id)
                failed.append(sub.name)
                event.last_error = f"{sub.name}: {type(e).__name__}"[:500]
        return failed

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
            failed_handlers=[],
            attempts=1,
        )
        session.add(event)
        session.flush()
        failed = self._run(session, event, self._subs.get(str(event_type), []))
        event.failed_handlers = failed
        if not failed:
            event.processed_at = utcnow()
        session.flush()
        return event

    def retry_failed(self, session: Session, *, now=None, limit: int = 100) -> int:
        """Re-run handlers that failed (best effort ones). Returns how many events were fully recovered."""
        now = now or utcnow()
        events = (
            session.execute(
                select(DomainEvent)
                .where(DomainEvent.processed_at.is_(None), DomainEvent.attempts < MAX_HANDLER_ATTEMPTS)
                .order_by(DomainEvent.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        recovered = 0
        for ev in events:
            # Exponential backoff based on the number of attempts (30s, 1m, 2m, ...).
            if now - ev.created_at < timedelta(seconds=30 * 2 ** max(0, ev.attempts - 1)):
                continue
            wanted = set(ev.failed_handlers or [])
            subs = [s for s in self._subs.get(ev.event_type, []) if s.name in wanted]
            ev.attempts += 1
            failed = self._run(session, ev, subs)
            # Handlers no longer registered cannot be retried: drop them.
            ev.failed_handlers = failed
            if not failed:
                ev.processed_at = now
                ev.last_error = None
                recovered += 1
        session.flush()
        return recovered


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
        from app.services.channel.service import register_event_handlers

        register_event_handlers(_bus)
    return _bus
