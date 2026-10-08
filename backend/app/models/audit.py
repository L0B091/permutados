from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, pg_enum
from app.models.enums import ActorType


class AuditLog(IdMixin, Base):
    """Append-only (UPDATE/DELETE blocked by a DB trigger, see migration)."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        Index("ix_audit_logs_action_created", "action", "created_at"),
    )

    action: Mapped[str] = mapped_column(String(60), nullable=False)
    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "actor_type"), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(40))
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    ip_address: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class DomainEvent(IdMixin, Base):
    """Persisted internal events (ITEM_COMPLETED, OFFER_CREATED, ...). Handlers run in-transaction."""

    __tablename__ = "domain_events"
    __table_args__ = (Index("ix_domain_events_type_created", "event_type", "created_at"),)

    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
