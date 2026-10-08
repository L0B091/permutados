from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin, pg_enum
from app.models.enums import InteractionKind, ItemChannelStatus, ItemStatus


class Item(IdMixin, TimestampMixin, Base):
    __tablename__ = "items"
    __table_args__ = (
        # Composite key so offers can reference (item_id, owner_id) and the DB guarantees
        # that offer.owner_id is always the real owner of the item.
        UniqueConstraint("id", "owner_id", name="uq_items_id_owner"),
        CheckConstraint("public_id ~ '^ITEM-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5,8}$'", name="public_id_format"),
        CheckConstraint("base_price IS NULL OR base_price > 0", name="price_positive"),
        CheckConstraint(
            "status = 'DRAFT' OR (title IS NOT NULL AND base_price IS NOT NULL AND zone_id IS NOT NULL "
            "AND accepts_swap IS NOT NULL AND has_damage IS NOT NULL AND works IS NOT NULL)",
            name="published_complete",
        ),
        CheckConstraint("NOT coalesce(has_damage, false) OR damage_description IS NOT NULL", name="damage_described"),
        CheckConstraint("coalesce(works, true) OR malfunction_description IS NOT NULL", name="malfunction_described"),
        CheckConstraint("status NOT IN ('PUBLISHED','RESERVED') OR expires_at IS NOT NULL", name="active_has_expiry"),
        Index("ix_items_status_published_at", "status", "published_at"),
        Index("ix_items_search_vector", "search_vector", postgresql_using="gin"),
    )

    public_id: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)  # ITEM-8F42K, immutable
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[ItemStatus] = mapped_column(
        pg_enum(ItemStatus, "item_status"), nullable=False, default=ItemStatus.DRAFT, server_default="DRAFT"
    )
    title: Mapped[str | None] = mapped_column(String(120))
    category: Mapped[str | None] = mapped_column(String(60), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    zone_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("zones.id", ondelete="RESTRICT"))
    base_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="ARS", server_default="ARS")
    accepts_swap: Mapped[bool | None] = mapped_column(Boolean)
    has_damage: Mapped[bool | None] = mapped_column(Boolean)
    damage_description: Mapped[str | None] = mapped_column(Text)
    works: Mapped[bool | None] = mapped_column(Boolean)
    malfunction_description: Mapped[str | None] = mapped_column(Text)
    # Suggestion from the vision model. Never contains a price; the user always confirms.
    ai_identification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    identification_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Normalised (lower-case, unaccented) text maintained by the service; used for search.
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('spanish', coalesce(search_text, ''))", persisted=True)
    )
    featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Channel status is separate from the publication status (section 33); maintained by the channel service.
    channel_status: Mapped[ItemChannelStatus] = mapped_column(
        pg_enum(ItemChannelStatus, "item_channel_status"),
        nullable=False,
        default=ItemChannelStatus.NOT_POSTED,
        server_default="NOT_POSTED",
    )
    score_breakdown: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # last computed signals (admin view)
    score_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attractiveness_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    selection_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    search_impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    offer_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    expiry_warning_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    images: Mapped[list[ItemImage]] = relationship(
        back_populates="item", order_by="ItemImage.position", cascade="all, delete-orphan", lazy="selectin"
    )


class ItemImage(IdMixin, TimestampMixin, Base):
    """Images of an item. The schema supports several; the MVP service allows exactly one."""

    __tablename__ = "item_images"
    __table_args__ = (
        UniqueConstraint("item_id", "position", name="uq_item_images_item_position"),
        CheckConstraint("position >= 0", name="position_nonneg"),
        CheckConstraint("mime_type IN ('image/jpeg','image/png','image/webp')", name="mime_allowed"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
    )

    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(20), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    quality_score: Mapped[float | None] = mapped_column(Numeric(6, 2))
    provider_media_id: Mapped[str | None] = mapped_column(String(128))

    item: Mapped[Item] = relationship(back_populates="images")


class ItemInteraction(Base):
    """Distinct-user interaction counters per item and day (abuse-resistant demand signals for the
    attractiveness score: one user spamming searches counts once per day). Owners are never recorded."""

    __tablename__ = "item_interactions"
    __table_args__ = (Index("ix_item_interactions_day", "day"),)

    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[InteractionKind] = mapped_column(pg_enum(InteractionKind, "interaction_kind"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
