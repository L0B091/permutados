"""Mercado Pago REST client: Checkout Pro preference + Payments / Merchant Orders APIs.

Endpoints (official API, https://api.mercadopago.com):
  POST /checkout/preferences          create the Checkout Pro link (X-Idempotency-Key = our external_reference)
  GET  /v1/payments/{id}              verify a payment notified by webhook (source of truth)
  GET  /v1/payments/search            reconcile by external_reference
  GET  /merchant_orders/{id}          resolve "merchant_order" notifications to their payments

Retries: GETs and the idempotent preference POST are retried on network errors, 429 and 5xx with
exponential backoff + jitter (injectable ``sleep``). 4xx are never retried.

NOT exercised against Mercado Pago in this repository (no credentials). Request/response shapes follow the
public API reference and are unit-tested with ``httpx.MockTransport``. Validate in the sandbox before go-live.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import ExternalServiceError
from app.services.mercadopago.gateway import CheckoutResult, ProviderPayment

API_BASE = "https://api.mercadopago.com"
SNAPSHOT_FIELDS = (
    "id",
    "status",
    "status_detail",
    "date_created",
    "date_approved",
    "transaction_amount",
    "currency_id",
    "external_reference",
    "live_mode",
    "payment_type_id",
    "payment_method_id",
    "collector_id",
    "operation_type",
)


def build_preference(
    *,
    external_reference: str,
    amount: Decimal,
    currency: str,
    title: str,
    notification_url: str,
    expires_at: datetime,
    back_url: str = "",
    statement_descriptor: str = "PERMUTADOS",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if amount <= 0:
        raise ValueError("amount must be positive")
    body: dict[str, Any] = {
        "items": [
            {
                "id": external_reference[:64],
                "title": title[:250],
                "description": "Tarifa de liberación de contacto (Permutados)",
                "category_id": "services",
                "quantity": 1,
                "currency_id": currency,
                "unit_price": float(amount),
            }
        ],
        "external_reference": external_reference,
        "notification_url": notification_url,
        "expires": True,
        "expiration_date_to": expires_at.isoformat(timespec="milliseconds"),
        "date_of_expiration": expires_at.isoformat(timespec="milliseconds"),
        # Only approved/rejected: no "pending" limbo; cash methods that settle days later are excluded because
        # the payment window is 24h.
        "binary_mode": True,
        "payment_methods": {"excluded_payment_types": [{"id": "ticket"}, {"id": "atm"}], "installments": 1},
        "statement_descriptor": statement_descriptor[:22],
        "metadata": metadata or {},
    }
    if back_url:
        body["back_urls"] = {"success": back_url, "failure": back_url, "pending": back_url}
        body["auto_return"] = "approved"
    return body


def parse_payment(data: dict[str, Any]) -> ProviderPayment:
    try:
        amount = Decimal(str(data.get("transaction_amount")))
    except (InvalidOperation, ValueError):
        amount = Decimal("-1")  # never matches an expected amount
    return ProviderPayment(
        provider_payment_id=str(data["id"]),
        status=str(data.get("status")),
        status_detail=data.get("status_detail"),
        amount=amount,
        currency=str(data.get("currency_id") or ""),
        external_reference=data.get("external_reference"),
        live_mode=data.get("live_mode"),
        collector_id=str(data["collector_id"]) if data.get("collector_id") is not None else None,
        raw={k: data.get(k) for k in SNAPSHOT_FIELDS},
    )


class MercadoPagoGateway:
    def __init__(
        self,
        access_token: str | None = None,
        *,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
        max_retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        s = get_settings()
        token = access_token or s.mercadopago_access_token.get_secret_value()
        if not token:
            raise ExternalServiceError("Mercado Pago is not configured", code="MP_NOT_CONFIGURED")
        self._client = httpx.Client(
            base_url=API_BASE, timeout=timeout, transport=transport, headers={"Authorization": f"Bearer {token}"}
        )
        self._max_retries = s.mercadopago_max_retries if max_retries is None else max_retries
        self._sleep = sleep

    def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        attempt = 0
        while True:
            try:
                r = self._client.request(method, url, **kw)
                if r.status_code != 429 and r.status_code < 500:
                    return r
                err: Exception = ExternalServiceError(f"Mercado Pago HTTP {r.status_code}", code="MP_UNAVAILABLE")
            except httpx.TransportError as e:
                err = e
            if attempt >= self._max_retries:
                raise ExternalServiceError("Mercado Pago no responde.", code="MP_UNAVAILABLE") from err
            self._sleep(min(8.0, 0.5 * 2**attempt) * (0.5 + random.random() / 2))  # noqa: S311
            attempt += 1

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
        s = get_settings()
        body = build_preference(
            external_reference=external_reference,
            amount=amount,
            currency=currency,
            title=title,
            notification_url=notification_url,
            expires_at=expires_at,
            back_url=s.mercadopago_back_url,
            statement_descriptor=s.mercadopago_statement_descriptor,
            metadata={"external_reference": external_reference},
        )
        r = self._request(
            "POST", "/checkout/preferences", json=body, headers={"X-Idempotency-Key": f"pref-{external_reference}"}
        )
        if r.status_code >= 400:
            raise ExternalServiceError("No se pudo crear el link de pago.", code="MP_CHECKOUT_FAILED")
        data = r.json()
        sandbox = s.mercadopago_environment == "sandbox"
        url = data.get("sandbox_init_point") if sandbox and data.get("sandbox_init_point") else data.get("init_point")
        if not url or not str(url).startswith("https://") or "id" not in data:
            raise ExternalServiceError("Respuesta inesperada de Mercado Pago.", code="MP_BAD_RESPONSE")
        return CheckoutResult(checkout_id=str(data["id"]), url=str(url))

    def get_payment(self, provider_payment_id: str) -> ProviderPayment | None:
        if not str(provider_payment_id).isdigit():
            return None
        r = self._request("GET", f"/v1/payments/{provider_payment_id}")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise ExternalServiceError("Mercado Pago devolvió un error.", code="MP_ERROR")
        return parse_payment(r.json())

    def search_payments(self, external_reference: str) -> list[ProviderPayment]:
        r = self._request(
            "GET",
            "/v1/payments/search",
            params={"external_reference": external_reference, "sort": "date_created", "criteria": "desc"},
        )
        if r.status_code >= 400:
            raise ExternalServiceError("Mercado Pago devolvió un error.", code="MP_ERROR")
        return [parse_payment(d) for d in r.json().get("results", [])]

    def get_merchant_order_payment_ids(self, order_id: str) -> list[str]:
        if not str(order_id).isdigit():
            return []
        r = self._request("GET", f"/merchant_orders/{order_id}")
        if r.status_code == 404:
            return []
        if r.status_code >= 400:
            raise ExternalServiceError("Mercado Pago devolvió un error.", code="MP_ERROR")
        return [str(p["id"]) for p in r.json().get("payments") or [] if p.get("id") is not None]
