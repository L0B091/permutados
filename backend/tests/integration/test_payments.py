from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.errors import Forbidden, InvalidState
from app.models.enums import (
    ItemStatus,
    OfferStatus,
    PaymentStatus,
    SecurityEventType,
    SystemMode,
    TransactionStatus,
)
from app.models.offer import Transaction
from app.models.payment import Payment
from app.models.security import SecurityEvent
from app.services.offers import service as offers
from app.services.payments import service as payments
from app.services.payments.service import Outcome
from app.services.system import config_service as cfg
from app.services.users.pii import reveal_notification_body


@pytest.fixture
def accepted(db, make_user, publish_item, gateway):
    owner, bidder = make_user("Owner"), make_user("Bidder")
    item = publish_item(owner, title="Bicicleta rodado 29")
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="140000")
    acc = offers.accept_offer(db, owner, res.offer.public_id)
    db.commit()
    return owner, bidder, item, res.offer, acc.transaction


def test_create_payment_link_only_for_bidder(db, accepted, gateway):
    owner, bidder, item, offer, tx = accepted
    with pytest.raises(Forbidden):
        payments.create_payment(db, owner, tx.public_id, gateway)  # the owner never pays
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    assert link.payment.amount == Decimal("3000.00") and link.payment.currency == "ARS"
    assert "ARS 3.000" in link.message and link.url.startswith("https://")
    again = payments.create_payment(db, bidder, tx.public_id, gateway)  # idempotent: reuses the open checkout
    assert again.payment.id == link.payment.id


def test_ya_pague_without_provider_confirmation_changes_nothing(db, accepted, gateway, notifications):
    owner, bidder, item, offer, tx = accepted
    payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    status = payments.get_payment_status(db, bidder, tx.public_id, gateway)  # user says "ya pagué"
    assert status["paid"] is False and status["status"] == "PAYMENT_PENDING"
    assert not notifications(bidder.id, "CONTACT_RELEASED_BIDDER")


def test_fake_payment_id_not_found_at_provider(db, accepted, gateway):
    assert payments.process_provider_notification(db, "123456789", gateway) == Outcome.NOT_FOUND_AT_PROVIDER


def test_pending_or_rejected_payment_does_not_release(db, accepted, gateway):
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    pp = gateway.simulate_payment(link.payment.external_reference, status="in_process")
    assert payments.process_provider_notification(db, pp.provider_payment_id, gateway) == Outcome.NOT_APPROVED
    pp = gateway.simulate_payment(link.payment.external_reference, status="rejected")
    assert payments.process_provider_notification(db, pp.provider_payment_id, gateway) == Outcome.REJECTED
    db.commit()
    db.refresh(tx)
    assert tx.status == TransactionStatus.PAYMENT_PENDING and tx.security_keyword is None


def test_approved_payment_releases_both_contacts_with_keyword(db, accepted, gateway, notifications):
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    assert payments.process_provider_notification(db, pp.provider_payment_id, gateway) == Outcome.CONFIRMED_AND_RELEASED
    db.commit()
    for o in (tx, offer, item):
        db.refresh(o)
    assert tx.status == TransactionStatus.CONTACT_RELEASED and offer.status == OfferStatus.CONTACT_RELEASED
    assert item.status == ItemStatus.COMPLETED
    kw = tx.security_keyword
    assert kw and kw.split("-")[0].isalpha() and kw.split("-")[1].isdigit()
    to_bidder = reveal_notification_body(notifications(bidder.id, "CONTACT_RELEASED_BIDDER")[0])
    to_owner = reveal_notification_body(notifications(owner.id, "CONTACT_RELEASED_OWNER")[0])
    assert f"+{owner.test_wa_id}" in to_bidder and kw in to_bidder
    assert f"+{bidder.test_wa_id}" in to_owner and kw in to_owner
    # Idempotent: the same webhook again does nothing new.
    assert payments.process_provider_notification(db, pp.provider_payment_id, gateway) == Outcome.ALREADY_PROCESSED
    db.commit()
    assert len(notifications(bidder.id, "CONTACT_RELEASED_BIDDER")) == 1


def test_amount_or_currency_mismatch_never_releases(db, accepted, gateway):
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    pp = gateway.simulate_payment(link.payment.external_reference, amount=Decimal("1.00"))
    assert payments.process_provider_notification(db, pp.provider_payment_id, gateway) == Outcome.MISMATCH
    db.commit()
    db.refresh(tx)
    assert tx.status == TransactionStatus.PAYMENT_PENDING
    assert db.execute(
        select(SecurityEvent).where(SecurityEvent.event_type == SecurityEventType.PAYMENT_MISMATCH)
    ).first()


def test_payment_after_owner_withdrew_is_flagged_for_refund(db, accepted, gateway, notifications):
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    offers.owner_withdraw(db, owner, offer.public_id)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)  # buyer paid anyway (race)
    assert payments.process_provider_notification(db, pp.provider_payment_id, gateway) == Outcome.NEEDS_REFUND
    db.commit()
    db.refresh(link.payment)
    assert link.payment.status == PaymentStatus.NEEDS_REFUND
    assert not notifications(bidder.id, "CONTACT_RELEASED_BIDDER")


def test_owner_withdraw_syncs_provider_first(db, accepted, gateway):
    """If the bidder already paid at Mercado Pago but the webhook did not arrive yet, the backend asks the
    provider before allowing the owner to back out."""
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    gateway.simulate_payment(link.payment.external_reference)
    payments.sync_transaction_payments(db, tx, gateway)
    db.commit()
    with pytest.raises(InvalidState):
        offers.owner_withdraw(db, owner, offer.public_id)


def test_keywords_unique_across_operations(db, make_user, publish_item, gateway, monkeypatch):
    owner = make_user()
    seq = iter(["LUNA-0001", "LUNA-0001", "LUNA-0002"])  # forced collision on the 2nd operation
    monkeypatch.setattr("app.services.payments.service.new_security_keyword", lambda: next(seq))
    keywords = []
    for i in range(2):
        bidder = make_user()
        item = publish_item(owner, title=f"Item {i}")
        res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="10")
        acc = offers.accept_offer(db, owner, res.offer.public_id)
        link = payments.create_payment(db, bidder, acc.transaction.public_id, gateway)
        db.commit()
        pp = gateway.simulate_payment(link.payment.external_reference)
        payments.process_provider_notification(db, pp.provider_payment_id, gateway)
        db.commit()
        keywords.append(db.get(Transaction, acc.transaction.id).security_keyword)
    assert keywords == ["LUNA-0001", "LUNA-0002"]


def test_release_requires_paid_state(db, accepted):
    owner, bidder, item, offer, tx = accepted
    with pytest.raises(InvalidState):
        payments.release_contact(db, tx)


def test_paused_mode_defers_release_until_online(db, accepted, gateway, notifications):
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    cfg.set_value(db, "system_mode", SystemMode.PAUSED.value)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    assert (
        payments.process_provider_notification(db, pp.provider_payment_id, gateway)
        == Outcome.CONFIRMED_RELEASE_DEFERRED
    )
    db.commit()
    db.refresh(tx)
    assert tx.status == TransactionStatus.PAID and not notifications(bidder.id, "CONTACT_RELEASED_BIDDER")
    assert payments.release_deferred(db) == 0  # still paused
    cfg.set_value(db, "system_mode", SystemMode.ONLINE.value)
    assert payments.release_deferred(db) == 1
    db.commit()
    db.refresh(tx)
    assert tx.status == TransactionStatus.CONTACT_RELEASED and notifications(bidder.id, "CONTACT_RELEASED_BIDDER")


def test_reconciliation_recovers_lost_webhook(db, accepted, gateway):
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    gateway.simulate_payment(link.payment.external_reference)  # webhook never arrives (server was down)
    assert payments.reconcile_pending_payments(db, gateway) == 1
    db.commit()
    db.refresh(tx)
    assert tx.status == TransactionStatus.CONTACT_RELEASED


def test_payment_window_job_never_cancels_a_paid_transaction(db, accepted, gateway):
    from datetime import timedelta

    from app.core.clock import utcnow

    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    cfg.set_value(db, "system_mode", SystemMode.PAUSED.value)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    payments.process_provider_notification(db, pp.provider_payment_id, gateway)
    db.commit()
    assert offers.expire_payment_windows(db, now=utcnow() + timedelta(days=3)) == 0
    db.commit()
    db.refresh(tx)
    assert tx.status == TransactionStatus.PAID
    assert db.execute(select(Payment.status)).scalar_one() == PaymentStatus.APPROVED
