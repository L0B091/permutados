from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin, pg_enum
from app.models.enums import ReportReason, ReportStatus


class Report(IdMixin, TimestampMixin, Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("item_id IS NOT NULL OR reported_user_id IS NOT NULL", name="has_target"),
        # A reporter cannot open the same report twice while it is open (anti-spam).
        Index(
            "uq_reports_open_per_reporter_item_reason",
            "reporter_id",
            "item_id",
            "reason",
            unique=True,
            postgresql_where=text("status IN ('OPEN','REVIEWING')"),
        ),
    )

    reporter_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="SET NULL"))
    reported_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[ReportReason] = mapped_column(pg_enum(ReportReason, "report_reason"), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ReportStatus] = mapped_column(
        pg_enum(ReportStatus, "report_status"), nullable=False, default=ReportStatus.OPEN, server_default="OPEN"
    )
    resolved_by_admin_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_notes: Mapped[str | None] = mapped_column(Text)
