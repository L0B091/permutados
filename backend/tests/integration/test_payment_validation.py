"""Phase 2 payment validations: operation/environment/collector checks and merchant_order notifications."""

import time

import pytest
from sqlalchemy import select

from app.core import db as dbmod
from app.core.config import get_settings
from app.models.enums import SecurityEventType, TransactionStatus
from app.models.offer import Transaction
from app.models.security import SecurityEvent
from app.services.mercadopago.signature import compute_signature
from app.services.offers import service as offers
from app.services.payments import service as payments
from app.services.payments.service import Outcome


@pytest.fixture
def pending(db, make_user, publish_item, gateway):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    acc = offers.accept_offer(db, owner, res.offer.public_id)
    link = payments.create_payment(db, bidder, acc.transaction.public_id, gateway)
    db.commit()
    return acc.transaction, link.payment


def test_checkout_uses_configured_fee(pending, gateway):
    tx, payment = pending
    [checkout] = gateway.checkouts.values()
    assert str(checkout["amount"]) == "3000.00" and checkout["currency"] == "ARS"
    assert payment.external_reference.startswith(tx.public_id + "-")


def test_sandbox_payment_rejected_in_production(db, pending, gateway, monkeypatch):
    tx, payment = pending
    monkeypatch.setattr(get_settings(), "environment", "production")
    pp = gateway.simulate_payment(payment.external_reference, live_mode=False)
    assert payments.process_provider_notification(db, pp.provider_payment_id, gateway) == Outcome.MISMATCH
    db.commit()
    db.refresh(tx)
    assert tx.status == TransactionStatus.PAYMENT_PENDING
    ev = db.execute(
        select(SecurityEvent).where(SecurityEvent.event_type == SecurityEventType.PAYMENT_MISMATCH)
    ).scalar_one()
    assert ev.details["reason"] == "sandbox_payment_in_production"


def test_payment_to_another_collector_rejected(db, pending, gateway, monkeypatch):
    tx, payment = pending
    monkeypatch.setattr(get_settings(), "mercadopago_collector_id", "111")
    pp = gateway.simulate_payment(payment.external_reference, collector_id="222")
    assert payments.process_provider_notification(db, pp.provider_payment_id, gateway) == Outcome.MISMATCH
    ok = gateway.simulate_payment(payment.external_reference, collector_id="111")
    assert payments.process_provider_notification(db, ok.provider_payment_id, gateway) == Outcome.CONFIRMED_AND_RELEASED


def test_wrong_amount_or_currency_never_releases(db, pending, gateway):
    from decimal import Decimal

    tx, payment = pending
    pp = gateway.simulate_payment(payment.external_reference, amount=Decimal("30.00"))
    assert payments.process_provider_notification(db, pp.provider_payment_id, gateway) == Outcome.MISMATCH
    pp = gateway.simulate_payment(payment.external_reference, currency="USD")
    assert payments.process_provider_notification(db, pp.provider_payment_id, gateway) in (
        Outcome.MISMATCH,
        Outcome.NEEDS_REFUND,
    )
    db.commit()
    db.refresh(tx)
    assert tx.status == TransactionStatus.PAYMENT_PENDING


def test_merchant_order_webhook_releases(client, pending, gateway):
    tx, payment = pending
    pp = gateway.simulate_payment(payment.external_reference)
    gateway.merchant_orders["555"] = [pp.provider_payment_id]
    ts = str(int(time.time()))
    sig = compute_signature("test-mp-webhook-secret", "555", "req-mo", ts)
    r = client.post(
        "/webhooks/mercadopago?data.id=555&type=merchant_order",
        json={"type": "merchant_order"},
        headers={"x-signature": f"ts={ts},v1={sig}", "x-request-id": "req-mo"},
    )
    assert r.status_code == 200
    with dbmod.session_scope() as s:
        assert s.get(Transaction, tx.id).status == TransactionStatus.CONTACT_RELEASED
