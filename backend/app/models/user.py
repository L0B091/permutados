from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin, pg_enum
from app.models.enums import UserStatus


class Zone(IdMixin, TimestampMixin, Base):
    """General zones (barrio / localidad). Only general zones are ever shown to other users."""

    __tablename__ = "zones"

    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    region: Mapped[str | None] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("warnings_count >= 0", name="warnings_nonneg"),
        CheckConstraint("evasion_strikes >= 0", name="strikes_nonneg"),
        CheckConstraint("whatsapp_hash ~ '^[0-9a-f]{64}$'", name="whatsapp_hash_hex"),
        CheckConstraint("whatsapp_last4 ~ '^[0-9]{4}$'", name="whatsapp_last4_digits"),
    )

    # WhatsApp ID (wa_id, the phone number in international format) is the primary external identity. It is
    # stored ENCRYPTED (AES-GCM, app.core.crypto) and looked up by a keyed blind index. Never resolve identity
    # by name. Decryption only through app.services.users.pii.
    whatsapp_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    whatsapp_enc: Mapped[str] = mapped_column(Text, nullable=False)
    whatsapp_last4: Mapped[str] = mapped_column(String(4), nullable=False)  # for admins: "•••1234"
    display_name_enc: Mapped[str | None] = mapped_column(Text)
    # Start of the free 24h customer-service window (last inbound message from this user).
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Last time a paid fallback template was sent (at most one per window, avoids template spam).
    last_template_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zone_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("zones.id", ondelete="SET NULL"))
    status: Mapped[UserStatus] = mapped_column(
        pg_enum(UserStatus, "user_status"), nullable=False, default=UserStatus.ACTIVE, server_default="ACTIVE"
    )
    warnings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    evasion_strikes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
