"""Item (publication) state machine. Every status change goes through ``transition_item``."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import InvalidState
from app.models.enums import ActorType, ItemStatus
from app.models.item import Item
from app.services.audit.service import AuditAction, audit
from app.services.events.bus import EventType, get_bus

ALLOWED: dict[ItemStatus, set[ItemStatus]] = {
    ItemStatus.DRAFT: {ItemStatus.PUBLISHED, ItemStatus.DELETED},
    ItemStatus.PUBLISHED: {ItemStatus.RESERVED, ItemStatus.EXPIRED, ItemStatus.DELETED},
    ItemStatus.RESERVED: {ItemStatus.PUBLISHED, ItemStatus.COMPLETED, ItemStatus.DELETED},
    ItemStatus.COMPLETED: set(),
    ItemStatus.EXPIRED: set(),
    ItemStatus.DELETED: set(),
}

_EVENTS = {
    ItemStatus.COMPLETED: (EventType.ITEM_COMPLETED, AuditAction.ITEM_COMPLETED),
    ItemStatus.EXPIRED: (EventType.ITEM_EXPIRED, AuditAction.ITEM_EXPIRED),
    ItemStatus.DELETED: (EventType.ITEM_DELETED, AuditAction.ITEM_DELETED),
    ItemStatus.RESERVED: (EventType.ITEM_STATUS_CHANGED, AuditAction.ITEM_PAUSED),
}


def transition_item(
    session: Session,
    item: Item,
    new: ItemStatus,
    *,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: int | str | None = None,
    reason: str | None = None,
) -> None:
    old = item.status
    if new not in ALLOWED[old]:
        raise InvalidState(
            f"item {item.public_id}: {old} -> {new} not allowed",
            code="ITEM_INVALID_TRANSITION",
            details={"from": old.value, "to": new.value},
        )
    now = utcnow()
    item.status = new
    if new == ItemStatus.COMPLETED:
        item.completed_at = now
    elif new == ItemStatus.DELETED:
        item.deleted_at = now
    session.flush()

    details = {"from": old.value, "to": new.value, **({"reason": reason} if reason else {})}
    if new == ItemStatus.PUBLISHED and old == ItemStatus.DRAFT:
        audit(
            session,
            AuditAction.ITEM_CREATED,
            actor_type=actor_type,
            actor_id=actor_id,
            entity_type="item",
            entity_id=item.public_id,
            details=details,
        )
        get_bus().publish(
            session, EventType.ITEM_CREATED, aggregate_type="item", aggregate_id=item.public_id, payload=details
        )
        return
    if new == ItemStatus.PUBLISHED:
        audit(
            session,
            AuditAction.ITEM_RESUMED,
            actor_type=actor_type,
            actor_id=actor_id,
            entity_type="item",
            entity_id=item.public_id,
            details=details,
        )
        get_bus().publish(
            session, EventType.ITEM_STATUS_CHANGED, aggregate_type="item", aggregate_id=item.public_id, payload=details
        )
        return
    event_type, action = _EVENTS[new]
    audit(
        session,
        action,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="item",
        entity_id=item.public_id,
        details=details,
    )
    if event_type != EventType.ITEM_STATUS_CHANGED:
        # Spec 31: closing transitions also emit the generic ITEM_STATUS_CHANGED.
        get_bus().publish(
            session, EventType.ITEM_STATUS_CHANGED, aggregate_type="item", aggregate_id=item.public_id, payload=details
        )
    get_bus().publish(session, event_type, aggregate_type="item", aggregate_id=item.public_id, payload=details)
