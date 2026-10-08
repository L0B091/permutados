"""Audit trail. Never stores secrets: sensitive keys are stripped before persisting."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.enums import ActorType


class AuditAction(StrEnum):
    USER_CREATED = "USER_CREATED"
    ITEM_DRAFT_CREATED = "ITEM_DRAFT_CREATED"
    ITEM_CREATED = "ITEM_CREATED"  # published
    ITEM_UPDATED = "ITEM_UPDATED"
    ITEM_DELETED = "ITEM_DELETED"
    ITEM_COMPLETED = "ITEM_COMPLETED"
    ITEM_EXPIRED = "ITEM_EXPIRED"
    ITEM_PAUSED = "ITEM_PAUSED"
    ITEM_RESUMED = "ITEM_RESUMED"
    OFFER_CREATED = "OFFER_CREATED"
    OFFER_ACCEPTED = "OFFER_ACCEPTED"
    OFFER_REJECTED = "OFFER_REJECTED"
    OFFER_EXPIRED = "OFFER_EXPIRED"
    OFFER_CANCELLED = "OFFER_CANCELLED"
    OFFER_WITHDRAWN_BY_OWNER = "OFFER_WITHDRAWN_BY_OWNER"
    TRANSACTION_EXPIRED = "TRANSACTION_EXPIRED"
    PAYMENT_CREATED = "PAYMENT_CREATED"
    PAYMENT_CONFIRMED = "PAYMENT_CONFIRMED"
    PAYMENT_REJECTED = "PAYMENT_REJECTED"
    PAYMENT_NEEDS_REFUND = "PAYMENT_NEEDS_REFUND"
    PAYMENT_VERIFICATION_FAILED = "PAYMENT_VERIFICATION_FAILED"
    CONTACT_RELEASED = "CONTACT_RELEASED"
    USER_WARNED = "USER_WARNED"
    USER_SUSPENDED = "USER_SUSPENDED"
    USER_BANNED = "USER_BANNED"
    USER_REINSTATED = "USER_REINSTATED"
    REPORT_CREATED = "REPORT_CREATED"
    CHANNEL_POST_CREATED = "CHANNEL_POST_CREATED"
    CHANNEL_POST_REMOVAL_REQUESTED = "CHANNEL_POST_REMOVAL_REQUESTED"
    CHANNEL_POST_REMOVED = "CHANNEL_POST_REMOVED"
    CHANNEL_POST_FAILED = "CHANNEL_POST_FAILED"
    CHANNEL_RECONCILIATION = "CHANNEL_RECONCILIATION"
    DOLPHIN_TOOL_DENIED = "DOLPHIN_TOOL_DENIED"
    ADMIN_ACTION = "ADMIN_ACTION"
    ADMIN_LOGIN = "ADMIN_LOGIN"
    ADMIN_LOGIN_FAILED = "ADMIN_LOGIN_FAILED"
    ADMIN_LOGOUT = "ADMIN_LOGOUT"
    ADMIN_VIEWED_USER = "ADMIN_VIEWED_USER"
    ADMIN_VIEWED_CONVERSATION = "ADMIN_VIEWED_CONVERSATION"
    ADMIN_CHANGED_CONFIG = "ADMIN_CHANGED_CONFIG"
    ADMIN_SUSPENDED_USER = "ADMIN_SUSPENDED_USER"
    ADMIN_BANNED_USER = "ADMIN_BANNED_USER"
    ADMIN_REINSTATED_USER = "ADMIN_REINSTATED_USER"
    ADMIN_REMOVED_ITEM = "ADMIN_REMOVED_ITEM"
    ADMIN_CHANGED_SYSTEM_MODE = "ADMIN_CHANGED_SYSTEM_MODE"
    ADMIN_CHANNEL_ACTION = "ADMIN_CHANNEL_ACTION"
    ADMIN_RESOLVED_REPORT = "ADMIN_RESOLVED_REPORT"
    SYSTEM_DEADLINES_EXTENDED = "SYSTEM_DEADLINES_EXTENDED"
    WORKER_RECOVERY = "WORKER_RECOVERY"
    WORKER_JOB = "WORKER_JOB"


_SENSITIVE = ("password", "token", "secret", "authorization", "api_key", "apikey", "access_key", "keyword")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if any(s in k.lower() for s in _SENSITIVE) else _sanitize(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def audit(
    session: Session,
    action: AuditAction | str,
    *,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: int | str | None = None,
    entity_type: str | None = None,
    entity_id: int | str | None = None,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditLog:
    entry = AuditLog(
        action=str(action),
        actor_type=actor_type,
        actor_id=str(actor_id) if actor_id is not None else None,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        details=_sanitize(details or {}),
        ip_address=ip_address,
    )
    session.add(entry)
    session.flush()
    return entry
