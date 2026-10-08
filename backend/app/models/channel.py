from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin, pg_enum
from app.models.enums import ChannelPostStatus


class ChannelPost(IdMixin, TimestampMixin, Base):
    """A post of an item in the WhatsApp Channel.

    The official Cloud API cannot post to Channels, so posts are created/removed manually
    by the admin; the backend records them and flags closed items as REMOVAL_PENDING.
    channel status is separate from item (publication) status.
    """

    __tablename__ = "channel_posts"
    __table_args__ = (
        Index(
            "uq_channel_posts_one_live_per_item",
            "item_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING','SENDING','PUBLISHED','RETRY_PENDING','REMOVAL_PENDING')"),
        ),
        Index("ix_channel_posts_status", "status"),
    )

    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[ChannelPostStatus] = mapped_column(pg_enum(ChannelPostStatus, "channel_post_status"), nullable=False)
    score_at_post: Mapped[int | None] = mapped_column(Integer)
    external_ref: Mapped[str | None] = mapped_column(String(255))  # link / note of the manual post
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by_admin_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    removal_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removal_reason: Mapped[str | None] = mapped_column(String(40))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_by_admin_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    last_error: Mapped[str | None] = mapped_column(Text)


class ChannelExposure(IdMixin, Base):
    __tablename__ = "channel_exposures"
    __table_args__ = (Index("ix_channel_exposures_item_exposed", "item_id", "exposed_at"),)

    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="CASCADE"), nullable=False)
    channel_post_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("channel_posts.id", ondelete="CASCADE"), nullable=False
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    position: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    exposed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)
