from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin, pg_enum
from app.models.enums import BanStatus, SecurityEventType, Severity


class SecurityEvent(IdMixin, Base):
    __tablename__ = "security_events"
    __table_args__ = (Index("ix_security_events_user_created", "user_id", "created_at"),)

    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    event_type: Mapped[SecurityEventType] = mapped_column(
        pg_enum(SecurityEventType, "security_event_type"), nullable=False
    )
    severity: Mapped[Severity] = mapped_column(pg_enum(Severity, "severity"), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    message_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("messages.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class Warning(IdMixin, Base):  # noqa: A001 - domain name from the spec
    __tablename__ = "warnings"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    message_sent: Mapped[str] = mapped_column(Text, nullable=False)
    security_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("security_events.id", ondelete="SET NULL")
    )
    issued_by_admin_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class Ban(IdMixin, TimestampMixin, Base):
    """Suspension / ban record. The backend only creates PENDING_REVIEW; an admin confirms or lifts."""

    __tablename__ = "bans"
    __table_args__ = (
        Index(
            "uq_bans_one_open_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING_REVIEW','CONFIRMED')"),
        ),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[BanStatus] = mapped_column(pg_enum(BanStatus, "ban_status"), nullable=False)
    reason: Mapped[str] = mapped_column(String(60), nullable=False)
    security_event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("security_events.id", ondelete="SET NULL")
    )
    reviewed_by_admin_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_notes: Mapped[str | None] = mapped_column(Text)
