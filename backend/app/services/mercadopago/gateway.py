"""Payment gateway interface. The backend only trusts data fetched from the provider API with our
credentials (never the webhook body, never the user, never Dolphin)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from app.models.enums import PaymentStatus

# Official Mercado Pago payment statuses -> internal.
MP_STATUS_MAP = {
    "approved": PaymentStatus.APPROVED,
    "authorized": PaymentStatus.PENDING,
    "pending": PaymentStatus.PENDING,
    "in_process": PaymentStatus.PENDING,
    "in_mediation": PaymentStatus.PENDING,
    "rejected": PaymentStatus.REJECTED,
    "cancelled": PaymentStatus.CANCELLED,
    "refunded": PaymentStatus.REFUNDED,
    "charged_back": PaymentStatus.REFUNDED,
}


@dataclass(frozen=True)
class CheckoutResult:
    checkout_id: str
    url: str


@dataclass(frozen=True)
class ProviderPayment:
    provider_payment_id: str
    status: str  # raw provider status, e.g. "approved"
    status_detail: str | None
    amount: Decimal
    currency: str
    external_reference: str | None
    live_mode: bool | None = None
    collector_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def internal_status(self) -> PaymentStatus:
        return MP_STATUS_MAP.get(self.status, PaymentStatus.PENDING)


class PaymentGateway(Protocol):
    def create_checkout(
        self,
        *,
        external_reference: str,
        amount: Decimal,
        currency: str,
        title: str,
        notification_url: str,
        expires_at: datetime,
    ) -> CheckoutResult: ...

    def get_payment(self, provider_payment_id: str) -> ProviderPayment | None: ...

    def search_payments(self, external_reference: str) -> list[ProviderPayment]: ...

    def get_merchant_order_payment_ids(self, order_id: str) -> list[str]: ...
