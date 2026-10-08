from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, TimestampMixin, pg_enum
from app.models.enums import MessageDirection, MessageType, NotificationStatus


class SearchSession(IdMixin, TimestampMixin, Base):
    __tablename__ = "search_sessions"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    query: Mapped[str] = mapped_column(Text, nullable=False)
    filters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Every item id ever shown in this search: guarantees no repetition across pages.
    shown_item_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, default=list, server_default="{}"
    )
    # Items of the last page in display order (1..5) for "el 2", "el segundo", ...
    last_page_item_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, default=list, server_default="{}"
    )
    exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")


class ConversationSession(IdMixin, TimestampMixin, Base):
    """Backend-owned conversation state (section 37). Dolphin only gets a filtered view."""

    __tablename__ = "conversation_sessions"
    __table_args__ = (
        Index("uq_conversation_sessions_active_user", "user_id", unique=True, postgresql_where=text("active")),
    )

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    conversation_state: Mapped[str] = mapped_column(String(40), nullable=False, default="IDLE", server_default="IDLE")
    current_intent: Mapped[str | None] = mapped_column(String(40))
    current_item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="SET NULL"))
    search_session_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("search_sessions.id", ondelete="SET NULL")
    )
    selected_item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="SET NULL"))
    current_offer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("offers.id", ondelete="SET NULL"))
    transaction_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("transactions.id", ondelete="SET NULL"))
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 24h customer-care window


class Message(IdMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "uq_messages_provider_message_id",
            "provider_message_id",
            unique=True,
            postgresql_where=text("provider_message_id IS NOT NULL"),
        ),
        Index("ix_messages_session_created", "session_id", "created_at"),
    )

    session_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("conversation_sessions.id", ondelete="SET NULL")
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(pg_enum(MessageDirection, "message_direction"), nullable=False)
    message_type: Mapped[MessageType] = mapped_column(pg_enum(MessageType, "message_type"), nullable=False)
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    body: Mapped[str | None] = mapped_column(Text)
    # Blocked messages (anti-evasion) keep NO plaintext: the evidence is encrypted for admin review.
    body_enc: Mapped[str | None] = mapped_column(Text)
    media: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    item_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("items.id", ondelete="SET NULL"))
    offer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("offers.id", ondelete="SET NULL"))
    transaction_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("transactions.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False)


class Notification(IdMixin, TimestampMixin, Base):
    """Outbox of messages to deliver via WhatsApp. ``idempotency_key`` prevents duplicates on retry."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_status_created", "status", "created_at"),
        Index("ix_notifications_dispatch", "status", "next_attempt_at"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    # For sensitive notifications (contact release) ``body`` holds a redacted placeholder and the rendered text
    # is in ``body_enc``; it is decrypted in memory only to hand it to WhatsApp.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_enc: Mapped[str | None] = mapped_column(Text)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    summary: Mapped[str | None] = mapped_column(String(200))  # PII-free text for the fallback template
    template_name: Mapped[str | None] = mapped_column(String(80))  # Meta template if outside the 24h window
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        pg_enum(NotificationStatus, "notification_status"),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default="PENDING",
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(500))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # don't deliver after this
    channel: Mapped[str | None] = mapped_column(String(20))  # SESSION | TEMPLATE (how it was delivered)
