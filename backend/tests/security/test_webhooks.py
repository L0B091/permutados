import json
import time

import pytest
from sqlalchemy import select

from app.core import db as dbmod
from app.models.enums import SecurityEventType, TransactionStatus
from app.models.offer import Transaction
from app.models.payment import WebhookEvent
from app.models.security import SecurityEvent
from app.services.mercadopago.signature import compute_signature
from app.services.offers import service as offers
from app.services.payments import service as payments
from app.services.whatsapp.signature import compute_signature as wa_sig

MP_SECRET = "test-mp-webhook-secret"


def mp_post(client, data_id, request_id="req-1", ts=None, secret=MP_SECRET, sig=None):
    ts = ts or str(int(time.time()))
    header = sig or f"ts={ts},v1={compute_signature(secret, data_id, request_id, ts)}"
    return client.post(
        f"/webhooks/mercadopago?data.id={data_id}&type=payment",
        json={"action": "payment.updated", "data": {"id": data_id}, "type": "payment"},
        headers={"x-signature": header, "x-request-id": request_id},
    )


@pytest.fixture
def paid_at_provider(db, make_user, publish_item, gateway):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    acc = offers.accept_offer(db, owner, res.offer.public_id)
    link = payments.create_payment(db, bidder, acc.transaction.public_id, gateway)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    return acc.transaction, pp


def tx_status(tx_id):
    with dbmod.session_scope() as s:
        return s.get(Transaction, tx_id).status


def test_mp_webhook_valid_releases(client, paid_at_provider):
    tx, pp = paid_at_provider
    assert mp_post(client, pp.provider_payment_id).status_code == 200
    assert tx_status(tx.id) == TransactionStatus.CONTACT_RELEASED


def test_mp_webhook_bad_signature_rejected(client, paid_at_provider):
    tx, pp = paid_at_provider
    assert mp_post(client, pp.provider_payment_id, secret="wrong").status_code == 401
    assert mp_post(client, pp.provider_payment_id, sig="ts=1,v1=deadbeef").status_code == 401
    assert tx_status(tx.id) == TransactionStatus.PAYMENT_PENDING
    with dbmod.session_scope() as s:
        assert s.execute(
            select(SecurityEvent).where(SecurityEvent.event_type == SecurityEventType.WEBHOOK_SIGNATURE_INVALID)
        ).first()


def test_mp_webhook_replay_old_timestamp_rejected(client, paid_at_provider):
    tx, pp = paid_at_provider
    old = str(int(time.time()) - 3600)
    assert mp_post(client, pp.provider_payment_id, ts=old).status_code == 401
    assert tx_status(tx.id) == TransactionStatus.PAYMENT_PENDING


def test_mp_webhook_duplicate_delivery_is_idempotent(client, paid_at_provider, notifications):
    tx, pp = paid_at_provider
    for _ in range(3):
        assert mp_post(client, pp.provider_payment_id, request_id="same").status_code == 200
    assert mp_post(client, pp.provider_payment_id, request_id="different-retry").status_code == 200
    assert len(notifications(kind="CONTACT_RELEASED_BIDDER")) == 1
    with dbmod.session_scope() as s:
        assert len(s.execute(select(WebhookEvent).where(WebhookEvent.provider == "mercadopago")).all()) == 2


def test_mp_webhook_forged_payment_id_does_nothing(client, paid_at_provider):
    tx, _ = paid_at_provider
    assert mp_post(client, "424242424242").status_code == 200  # validly signed but unknown at provider
    assert tx_status(tx.id) == TransactionStatus.PAYMENT_PENDING


def test_mp_provider_down_returns_502_and_retry_works(client, paid_at_provider, gateway):
    tx, pp = paid_at_provider
    gateway.fail = True
    assert mp_post(client, pp.provider_payment_id, request_id="r1").status_code == 502
    gateway.fail = False
    assert mp_post(client, pp.provider_payment_id, request_id="r1").status_code == 200
    assert tx_status(tx.id) == TransactionStatus.CONTACT_RELEASED


def test_whatsapp_verify_handshake(client):
    ok = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "test-verify-token", "hub.challenge": "12345"},
    )
    assert ok.status_code == 200 and ok.text == "12345"
    assert (
        client.get(
            "/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "x", "hub.challenge": "1"}
        ).status_code
        == 403
    )


def _wa_payload(msg_id="wamid.A"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "5491100000001", "profile": {"name": "Ana"}}],
                            "messages": [
                                {
                                    "from": "5491100000001",
                                    "id": msg_id,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": "hola"},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


def test_whatsapp_signature_and_dedupe(client):
    body = json.dumps(_wa_payload()).encode()
    bad = client.post(
        "/webhooks/whatsapp",
        content=body,
        headers={"x-hub-signature-256": "sha256=00", "content-type": "application/json"},
    )
    assert bad.status_code == 401
    sig = wa_sig("test-wa-app-secret", body)
    h = {"x-hub-signature-256": sig, "content-type": "application/json"}
    assert client.post("/webhooks/whatsapp", content=body, headers=h).json() == {"received": 1}
    assert client.post("/webhooks/whatsapp", content=body, headers=h).json() == {"received": 0}
