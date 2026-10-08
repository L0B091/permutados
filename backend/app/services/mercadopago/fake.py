"""In-memory gateway for tests and local development. NEVER used in production
(the app factory refuses it when ENVIRONMENT=production)."""

from __future__ import annotations

import itertools
from datetime import datetime
from decimal import Decimal

from app.services.mercadopago.gateway import CheckoutResult, ProviderPayment


class FakeMercadoPagoGateway:
    def __init__(self) -> None:
        self.checkouts: dict[str, dict] = {}
        self.payments: dict[str, ProviderPayment] = {}
        self._ids = itertools.count(9_000_000_001)
        self.fail = False

    def create_checkout(
        self,
        *,
        external_reference: str,
        amount: Decimal,
        currency: str,
        title: str,
        notification_url: str,
        expires_at: datetime,
    ) -> CheckoutResult:
        if self.fail:
            from app.core.errors import ExternalServiceError

            raise ExternalServiceError("No se pudo crear el link de pago.", code="MP_CHECKOUT_FAILED")
        cid = f"pref-{len(self.checkouts) + 1}"
        self.checkouts[cid] = {"external_reference": external_reference, "amount": amount, "currency": currency}
        return CheckoutResult(checkout_id=cid, url=f"https://sandbox.mercadopago.test/checkout/{cid}")

    def simulate_payment(
        self, external_reference: str, *, status: str = "approved", amount: Decimal | None = None, currency: str = "ARS"
    ) -> ProviderPayment:
        """Simulate the buyer paying at the provider (what Mercado Pago would record)."""
        checkout = next((c for c in self.checkouts.values() if c["external_reference"] == external_reference), None)
        amt = amount if amount is not None else (checkout["amount"] if checkout else Decimal("1"))
        pid = str(next(self._ids))
        p = ProviderPayment(
            provider_payment_id=pid,
            status=status,
            status_detail="accredited" if status == "approved" else status,
            amount=Decimal(str(amt)),
            currency=currency,
            external_reference=external_reference,
            live_mode=False,
        )
        self.payments[pid] = p
        return p

    def get_payment(self, provider_payment_id: str) -> ProviderPayment | None:
        if self.fail:
            from app.core.errors import ExternalServiceError

            raise ExternalServiceError("Mercado Pago no responde.", code="MP_UNAVAILABLE")
        return self.payments.get(str(provider_payment_id))

    def search_payments(self, external_reference: str) -> list[ProviderPayment]:
        if self.fail:
            from app.core.errors import ExternalServiceError

            raise ExternalServiceError("Mercado Pago no responde.", code="MP_UNAVAILABLE")
        return [p for p in self.payments.values() if p.external_reference == external_reference]
