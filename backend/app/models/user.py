from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Integer, String
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
        CheckConstraint("whatsapp_id ~ '^[0-9]{6,20}$'", name="whatsapp_id_digits"),
    )

    # WhatsApp ID (wa_id) is the primary external identity. Never resolve identity by name.
    whatsapp_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    display_name: Mapped[str | None] = mapped_column(String(120))
    zone_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("zones.id", ondelete="SET NULL"))
    status: Mapped[UserStatus] = mapped_column(
        pg_enum(UserStatus, "user_status"), nullable=False, default=UserStatus.ACTIVE, server_default="ACTIVE"
    )
    warnings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    evasion_strikes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
