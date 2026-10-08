from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin, pg_enum
from app.models.enums import OfferCloseReason, OfferStatus, OfferType, TransactionStatus


class Offer(IdMixin, TimestampMixin, Base):
    __tablename__ = "offers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["item_id", "owner_id"],
            ["items.id", "items.owner_id"],
            name="fk_offers_item_owner_items",
            ondelete="RESTRICT",
        ),
        CheckConstraint("bidder_id <> owner_id", name="not_self_offer"),
        CheckConstraint("offered_price IS NULL OR offered_price > 0", name="price_positive"),
        CheckConstraint(
            "(offer_type = 'MONEY' AND offered_price IS NOT NULL AND offered_item IS NULL) OR "
            "(offer_type = 'SWAP' AND offered_price IS NULL AND offered_item IS NOT NULL) OR "
            "(offer_type = 'COMBINED' AND offered_price IS NOT NULL AND offered_item IS NOT NULL)",
            name="type_consistent",
        ),
        CheckConstraint("expires_at > created_at", name="expiry_after_creation"),
        # THE invariant: at most one active offer per item, enforced by PostgreSQL.
        Index(
            "uq_offers_one_active_per_item",
            "item_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING','ACCEPTED','PAYMENT_PENDING','PAID')"),
        ),
        Index("ix_offers_status_expires_at", "status", "expires_at"),
    )

    public_id: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False)
    bidder_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    offer_type: Mapped[OfferType] = mapped_column(pg_enum(OfferType, "offer_type"), nullable=False)
    offered_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    offered_item: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[OfferStatus] = mapped_column(
        pg_enum(OfferStatus, "offer_status"), nullable=False, default=OfferStatus.PENDING, server_default="PENDING"
    )
    close_reason: Mapped[OfferCloseReason | None] = mapped_column(pg_enum(OfferCloseReason, "offer_close_reason"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Transaction(IdMixin, TimestampMixin, Base):
    """Created when the owner accepts an offer. Tracks the release-fee payment and contact release."""

    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "status <> 'CONTACT_RELEASED' OR (security_keyword IS NOT NULL AND released_at IS NOT NULL "
            "AND paid_at IS NOT NULL)",
            name="release_requires_payment",
        ),
        Index("ix_transactions_status_due", "status", "payment_due_at"),
    )

    public_id: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    offer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("offers.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False)
    owner_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    bidder_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[TransactionStatus] = mapped_column(
        pg_enum(TransactionStatus, "transaction_status"),
        nullable=False,
        default=TransactionStatus.PAYMENT_PENDING,
        server_default="PAYMENT_PENDING",
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)  # snapshot at acceptance
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payment_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    security_keyword: Mapped[str | None] = mapped_column(String(32), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(String(40))
