"""Real MercadoPagoGateway against httpx.MockTransport (request/response shapes, retries)."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from app.core.errors import ExternalServiceError
from app.services.mercadopago.client import MercadoPagoGateway, build_preference


class Recorder:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request):
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make(responses, retries=2):
    rec = Recorder(responses)
    sleeps = []
    gw = MercadoPagoGateway(
        "APP-test-token", transport=httpx.MockTransport(rec), max_retries=retries, sleep=sleeps.append
    )
    return gw, rec, sleeps


DUE = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)


def test_create_checkout_request_shape():
    gw, rec, _ = make(
        [
            httpx.Response(
                201,
                json={
                    "id": "123-pref",
                    "init_point": "https://www.mercadopago.com.ar/c",
                    "sandbox_init_point": "https://sandbox.mercadopago.com.ar/c",
                },
            )
        ]
    )
    res = gw.create_checkout(
        external_reference="OP-ABCDE-1a2b",
        amount=Decimal("3000.00"),
        currency="ARS",
        title="Permutados - liberación",
        notification_url="https://api.x/webhooks/mercadopago",
        expires_at=DUE,
    )
    assert res.checkout_id == "123-pref" and res.url == "https://sandbox.mercadopago.com.ar/c"  # sandbox env
    req = rec.requests[0]
    assert req.method == "POST" and req.url.path == "/checkout/preferences"
    assert req.headers["authorization"] == "Bearer APP-test-token"
    assert req.headers["x-idempotency-key"] == "pref-OP-ABCDE-1a2b"  # deterministic -> safe retries
    body = json.loads(req.content)
    assert body["items"][0]["unit_price"] == 3000.0 and body["items"][0]["currency_id"] == "ARS"
    assert body["items"][0]["quantity"] == 1
    assert body["external_reference"] == "OP-ABCDE-1a2b"
    assert body["notification_url"] == "https://api.x/webhooks/mercadopago"
    assert body["binary_mode"] is True and body["expires"] is True
    assert body["expiration_date_to"].startswith("2026-10-09T15:00:00.000")
    assert {"id": "ticket"} in body["payment_methods"]["excluded_payment_types"]
    assert "back_urls" not in body


def test_build_preference_with_back_url_and_validation():
    b = build_preference(
        external_reference="r",
        amount=Decimal("3000"),
        currency="ARS",
        title="t",
        notification_url="https://n",
        expires_at=DUE,
        back_url="https://permutados.example/ok",
    )
    assert b["auto_return"] == "approved" and b["back_urls"]["success"] == "https://permutados.example/ok"
    with pytest.raises(ValueError):
        build_preference(
            external_reference="r",
            amount=Decimal("0"),
            currency="ARS",
            title="t",
            notification_url="https://n",
            expires_at=DUE,
        )


def test_create_checkout_retries_then_fails_cleanly():
    gw, rec, sleeps = make([httpx.Response(503), httpx.ConnectError("x"), httpx.Response(500)])
    with pytest.raises(ExternalServiceError) as ei:
        gw.create_checkout(
            external_reference="r",
            amount=Decimal("3000"),
            currency="ARS",
            title="t",
            notification_url="https://n",
            expires_at=DUE,
        )
    assert ei.value.code == "MP_UNAVAILABLE" and len(rec.requests) == 3 and len(sleeps) == 2
    gw, _, _ = make([httpx.Response(400, json={"message": "invalid"})])
    with pytest.raises(ExternalServiceError) as ei:
        gw.create_checkout(
            external_reference="r",
            amount=Decimal("3000"),
            currency="ARS",
            title="t",
            notification_url="https://n",
            expires_at=DUE,
        )
    assert ei.value.code == "MP_CHECKOUT_FAILED"


PAYMENT = {
    "id": 1234567890,
    "status": "approved",
    "status_detail": "accredited",
    "transaction_amount": 3000,
    "currency_id": "ARS",
    "external_reference": "OP-ABCDE-1a2b",
    "live_mode": True,
    "collector_id": 99,
    "payer": {"email": "someone@example.com"},
    "card": {"last_four_digits": "1234"},
}


def test_get_payment_parses_and_snapshots_without_payer_data():
    gw, rec, _ = make([httpx.Response(200, json=PAYMENT)])
    p = gw.get_payment("1234567890")
    assert rec.requests[0].url.path == "/v1/payments/1234567890"
    assert p.provider_payment_id == "1234567890" and p.status == "approved"
    assert p.amount == Decimal("3000") and p.currency == "ARS" and p.collector_id == "99" and p.live_mode is True
    assert "payer" not in p.raw and "card" not in p.raw


def test_get_payment_edge_cases():
    gw, rec, _ = make([])
    assert gw.get_payment("abc'; DROP") is None and not rec.requests
    gw, _, _ = make([httpx.Response(404, json={})])
    assert gw.get_payment("1") is None
    gw, rec, sleeps = make([httpx.Response(502), httpx.Response(200, json=PAYMENT)])
    assert gw.get_payment("1234567890").status == "approved" and len(sleeps) == 1
    gw, _, _ = make([httpx.Response(200, json={**PAYMENT, "transaction_amount": "garbage"})])
    assert gw.get_payment("1").amount == Decimal("-1")


def test_search_and_merchant_order():
    gw, rec, _ = make(
        [
            httpx.Response(200, json={"results": [PAYMENT]}),
            httpx.Response(200, json={"id": 77, "payments": [{"id": 1}, {"id": 2}]}),
        ]
    )
    [p] = gw.search_payments("OP-ABCDE-1a2b")
    assert rec.requests[0].url.params["external_reference"] == "OP-ABCDE-1a2b" and p.status == "approved"
    assert gw.get_merchant_order_payment_ids("77") == ["1", "2"]
    assert rec.requests[1].url.path == "/merchant_orders/77"
