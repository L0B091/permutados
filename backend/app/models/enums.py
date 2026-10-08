"""State enums. Stored as native PostgreSQL ENUM types."""

from __future__ import annotations

from enum import StrEnum


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"  # pending admin review (2nd evasion attempt) or manual
    BANNED = "BANNED"  # confirmed by an admin


class ItemStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RESERVED = "RESERVED"  # paused: an offer is pending (24h owner window) or awaiting payment
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    DELETED = "DELETED"


class OfferType(StrEnum):
    MONEY = "MONEY"
    SWAP = "SWAP"
    COMBINED = "COMBINED"


class OfferStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    CONTACT_RELEASED = "CONTACT_RELEASED"
    CANCELLED = "CANCELLED"


ACTIVE_OFFER_STATUSES = (
    OfferStatus.PENDING,
    OfferStatus.ACCEPTED,
    OfferStatus.PAYMENT_PENDING,
    OfferStatus.PAID,
)


class OfferCloseReason(StrEnum):
    BIDDER_CANCELLED = "BIDDER_CANCELLED"
    OWNER_WITHDREW = "OWNER_WITHDREW"
    PAYMENT_TIMEOUT = "PAYMENT_TIMEOUT"
    ITEM_UNAVAILABLE = "ITEM_UNAVAILABLE"
    USER_BLOCKED = "USER_BLOCKED"
    ADMIN = "ADMIN"


class TransactionStatus(StrEnum):
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    CONTACT_RELEASED = "CONTACT_RELEASED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class PaymentStatus(StrEnum):
    CREATED = "CREATED"  # checkout created, not paid
    PENDING = "PENDING"  # provider reports pending/in_process
    APPROVED = "APPROVED"  # provider-verified approved
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    NEEDS_REFUND = "NEEDS_REFUND"  # approved after the operation was closed -> admin must refund


class PaymentProvider(StrEnum):
    MERCADOPAGO = "MERCADOPAGO"


class BanStatus(StrEnum):
    PENDING_REVIEW = "PENDING_REVIEW"
    CONFIRMED = "CONFIRMED"
    LIFTED = "LIFTED"


class SecurityEventType(StrEnum):
    EVASION_ATTEMPT = "EVASION_ATTEMPT"
    AI_FLAGGED_SUSPICION = "AI_FLAGGED_SUSPICION"
    PROMPT_INJECTION_SUSPECTED = "PROMPT_INJECTION_SUSPECTED"
    TOOL_DENIED = "TOOL_DENIED"
    WEBHOOK_SIGNATURE_INVALID = "WEBHOOK_SIGNATURE_INVALID"
    WEBHOOK_REPLAY = "WEBHOOK_REPLAY"
    PAYMENT_MISMATCH = "PAYMENT_MISMATCH"
    PAYMENT_AFTER_CLOSE = "PAYMENT_AFTER_CLOSE"
    RATE_LIMITED = "RATE_LIMITED"
    ADMIN_LOGIN_FAILED = "ADMIN_LOGIN_FAILED"


class Severity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ReportReason(StrEnum):
    FRAUD = "FRAUD"
    FAKE_ITEM = "FAKE_ITEM"
    PROHIBITED_ITEM = "PROHIBITED_ITEM"
    SPAM = "SPAM"
    WRONG_INFO = "WRONG_INFO"
    SUSPICIOUS_BEHAVIOR = "SUSPICIOUS_BEHAVIOR"


class ReportStatus(StrEnum):
    OPEN = "OPEN"
    REVIEWING = "REVIEWING"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class ChannelPostStatus(StrEnum):
    # Full lifecycle from the spec. With manual administration the admin moves posts
    # PENDING -> PUBLISHED (registered as posted) and REMOVAL_PENDING -> REMOVED.
    PENDING = "PENDING"
    SENDING = "SENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    RETRY_PENDING = "RETRY_PENDING"
    REMOVAL_PENDING = "REMOVAL_PENDING"
    REMOVED = "REMOVED"


LIVE_CHANNEL_POST_STATUSES = (
    ChannelPostStatus.PENDING,
    ChannelPostStatus.SENDING,
    ChannelPostStatus.PUBLISHED,
    ChannelPostStatus.RETRY_PENDING,
    ChannelPostStatus.REMOVAL_PENDING,
)


class MessageDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class MessageType(StrEnum):
    TEXT = "TEXT"
    IMAGE = "IMAGE"
    INTERACTIVE = "INTERACTIVE"
    SYSTEM = "SYSTEM"
    OTHER = "OTHER"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class AdminRole(StrEnum):
    SUPERADMIN = "SUPERADMIN"
    ADMIN = "ADMIN"
    MODERATOR = "MODERATOR"
    VIEWER = "VIEWER"


class SystemMode(StrEnum):
    ONLINE = "ONLINE"
    PAUSED = "PAUSED"
    EMERGENCY = "EMERGENCY"


class ActorType(StrEnum):
    SYSTEM = "SYSTEM"
    USER = "USER"
    ADMIN = "ADMIN"
    WORKER = "WORKER"
    WEBHOOK = "WEBHOOK"
