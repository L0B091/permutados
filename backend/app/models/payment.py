from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin, pg_enum
from app.models.enums import PaymentProvider, PaymentStatus


class Payment(IdMixin, TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        # At most one approved payment per transaction.
        Index(
            "uq_payments_one_approved_per_tx",
            "transaction_id",
            unique=True,
            postgresql_where=text("status = 'APPROVED'"),
        ),
        Index(
            "uq_payments_provider_payment",
            "provider",
            "provider_payment_id",
            unique=True,
            postgresql_where=text("provider_payment_id IS NOT NULL"),
        ),
    )

    transaction_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    payer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    provider: Mapped[PaymentProvider] = mapped_column(pg_enum(PaymentProvider, "payment_provider"), nullable=False)
    # Our reference sent to the provider; used to map provider payments back to us.
    external_reference: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    provider_checkout_id: Mapped[str | None] = mapped_column(String(128))  # e.g. MP preference id
    provider_payment_id: Mapped[str | None] = mapped_column(String(64))
    checkout_url: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[PaymentStatus] = mapped_column(
        pg_enum(PaymentStatus, "payment_status"),
        nullable=False,
        default=PaymentStatus.CREATED,
        server_default="CREATED",
    )
    provider_status: Mapped[str | None] = mapped_column(String(40))
    provider_status_detail: Mapped[str | None] = mapped_column(String(80))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    live_mode: Mapped[bool | None] = mapped_column(Boolean)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # sanitised, no secrets


class WebhookEvent(IdMixin, Base):
    """Idempotency / replay ledger for inbound webhooks."""

    __tablename__ = "webhook_events"

    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="RECEIVED", server_default="RECEIVED")
    error: Mapped[str | None] = mapped_column(String(500))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (Index("uq_webhook_events_provider_key", "provider", "dedupe_key", unique=True),)
