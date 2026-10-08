"""Mercado Pago REST client (Checkout Pro preference + Payments API).

Endpoints used (official API, https://api.mercadopago.com):
  POST /checkout/preferences          create the checkout link
  GET  /v1/payments/{id}              verify a payment notified by webhook
  GET  /v1/payments/search            reconcile by external_reference

PHASE 1 NOTE: implemented against the public documentation but NOT yet exercised with real
credentials. Phase 2 validates it in the sandbox before production use.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import httpx

from app.core.config import get_settings
from app.core.errors import ExternalServiceError
from app.services.mercadopago.gateway import CheckoutResult, ProviderPayment

API_BASE = "https://api.mercadopago.com"


class MercadoPagoGateway:
    def __init__(
        self, access_token: str | None = None, *, timeout: float = 15.0, transport: httpx.BaseTransport | None = None
    ):
        token = access_token or get_settings().mercadopago_access_token.get_secret_value()
        if not token:
            raise ExternalServiceError("Mercado Pago is not configured", code="MP_NOT_CONFIGURED")
        self._client = httpx.Client(
            base_url=API_BASE, timeout=timeout, transport=transport, headers={"Authorization": f"Bearer {token}"}
        )

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
        body = {
            "items": [{"title": title[:250], "quantity": 1, "unit_price": float(amount), "currency_id": currency}],
            "external_reference": external_reference,
            "notification_url": notification_url,
            "expires": True,
            "expiration_date_to": expires_at.isoformat(timespec="milliseconds"),
            "binary_mode": True,  # only approved / rejected, no pending limbo
        }
        try:
            r = self._client.post(
                "/checkout/preferences",
                json=body,
                headers={"X-Idempotency-Key": f"pref-{external_reference}-{uuid.uuid4().hex[:8]}"},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ExternalServiceError("No se pudo crear el link de pago.", code="MP_CHECKOUT_FAILED") from e
        data = r.json()
        sandbox = get_settings().mercadopago_environment == "sandbox"
        url = data.get("sandbox_init_point") if sandbox and data.get("sandbox_init_point") else data["init_point"]
        return CheckoutResult(checkout_id=str(data["id"]), url=url)

    @staticmethod
    def _parse(data: dict) -> ProviderPayment:
        return ProviderPayment(
            provider_payment_id=str(data["id"]),
            status=str(data.get("status")),
            status_detail=data.get("status_detail"),
            amount=Decimal(str(data.get("transaction_amount"))),
            currency=str(data.get("currency_id")),
            external_reference=data.get("external_reference"),
            live_mode=data.get("live_mode"),
            raw={
                k: data.get(k)
                for k in (
                    "id",
                    "status",
                    "status_detail",
                    "date_approved",
                    "transaction_amount",
                    "currency_id",
                    "external_reference",
                    "live_mode",
                    "payment_type_id",
                )
            },
        )

    def get_payment(self, provider_payment_id: str) -> ProviderPayment | None:
        if not str(provider_payment_id).isdigit():
            return None
        try:
            r = self._client.get(f"/v1/payments/{provider_payment_id}")
        except httpx.HTTPError as e:
            raise ExternalServiceError("Mercado Pago no responde.", code="MP_UNAVAILABLE") from e
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise ExternalServiceError("Mercado Pago devolvió un error.", code="MP_ERROR")
        return self._parse(r.json())

    def search_payments(self, external_reference: str) -> list[ProviderPayment]:
        try:
            r = self._client.get(
                "/v1/payments/search",
                params={"external_reference": external_reference, "sort": "date_created", "criteria": "desc"},
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ExternalServiceError("Mercado Pago no responde.", code="MP_UNAVAILABLE") from e
        return [self._parse(d) for d in r.json().get("results", [])]
