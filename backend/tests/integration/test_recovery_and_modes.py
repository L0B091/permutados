"""Downtime recovery for payments and system-mode enforcement across workers (real PostgreSQL)."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.core import db as dbmod
from app.core.clock import utcnow
from app.models.audit import AuditLog
from app.models.channel import ChannelPost
from app.models.conversation import Notification
from app.models.enums import (
    AdminRole,
    ChannelPostStatus,
    ItemStatus,
    JobRunStatus,
    OfferStatus,
    SystemMode,
    TransactionStatus,
)
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.payment import Payment
from app.services.admin.auth import create_admin
from app.services.channel import service as channel
from app.services.offers import service as offers
from app.services.payments import service as payments
from app.services.system import config_service as cfg
from app.workers.jobs import maintenance_jobs
from app.workers.runtime import run_job


@pytest.fixture
def accepted(db, make_user, publish_item, gateway):
    owner, bidder = make_user("Owner"), make_user("Bidder")
    item = publish_item(owner, title="Bicicleta rodado 29")
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="140000")
    acc = offers.accept_offer(db, owner, res.offer.public_id)
    db.commit()
    return owner, bidder, item, res.offer, acc.transaction


def _overdue(db, tx, hours=1):
    db.execute(
        update(Transaction).where(Transaction.id == tx.id).values(payment_due_at=utcnow() - timedelta(hours=hours))
    )
    db.commit()


def _fresh(db, model, pk):
    db.expire_all()
    return db.get(model, pk)


def _count(db, kind):
    db.expire_all()
    return db.execute(select(func.count()).select_from(Notification).where(Notification.kind == kind)).scalar_one()


# ---- payment window expiry vs downtime ---------------------------------------------------------------------
def test_payment_approved_during_downtime_is_honoured_not_cancelled(
    db, make_user, publish_item, gateway, notifications
):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    admin = create_admin(db, "root", "root-password-123456", AdminRole.ADMIN)
    post = channel.register_manual_post(db, admin.id, item.public_id, external_ref=None)
    offer = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000").offer
    tx = offers.accept_offer(db, owner, offer.public_id).transaction
    db.commit()
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    gateway.simulate_payment(link.payment.external_reference)  # paid; the webhook never reached us
    _overdue(db, tx)
    assert offers.expire_payment_windows(db, gateway=gateway) == 0
    db.commit()
    assert _fresh(db, Transaction, tx.id).status == TransactionStatus.CONTACT_RELEASED
    assert _fresh(db, Offer, offer.id).status != OfferStatus.CANCELLED
    assert _fresh(db, Item, item.id).status == ItemStatus.COMPLETED
    assert _fresh(db, ChannelPost, post.id).status == ChannelPostStatus.REMOVAL_PENDING
    assert notifications(bidder.id, "CONTACT_RELEASED_BIDDER") and not notifications(
        bidder.id, "PAYMENT_TIMEOUT_BIDDER"
    )


def test_provider_unreachable_postpones_expiry(db, accepted, gateway):
    owner, bidder, item, offer, tx = accepted
    payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    _overdue(db, tx)
    gateway.fail = True
    assert offers.expire_payment_windows(db, gateway=gateway) == 0
    db.commit()
    assert _fresh(db, Transaction, tx.id).status == TransactionStatus.PAYMENT_PENDING  # never decide on ambiguity
    gateway.fail = False
    assert offers.expire_payment_windows(db, gateway=gateway) == 1
    db.commit()
    assert _fresh(db, Offer, offer.id).status == OfferStatus.CANCELLED
    assert _fresh(db, Item, item.id).status == ItemStatus.PUBLISHED  # item back in the catalogue


def test_payment_in_process_at_provider_is_waited_for_bounded(db, accepted, gateway):
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    gateway.simulate_payment(link.payment.external_reference, status="in_process")
    _overdue(db, tx)
    assert offers.expire_payment_windows(db, gateway=gateway) == 0
    db.commit()
    assert _fresh(db, Transaction, tx.id).status == TransactionStatus.PAYMENT_PENDING
    _overdue(db, tx, hours=73)
    assert offers.expire_payment_windows(db, gateway=gateway) == 1
    db.commit()
    assert _fresh(db, Offer, offer.id).status == OfferStatus.CANCELLED


def test_reconciliation_and_late_webhook_release_exactly_once(db, accepted, gateway):
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    job = next(j for j in maintenance_jobs(gateway) if j.name == "reconcile_pending_payments")
    assert run_job(job, "w").status == JobRunStatus.SUCCEEDED
    assert run_job(job, "w").affected == 0  # second pass: nothing left
    with dbmod.session_scope() as s:  # the lost webhook finally arrives
        payments.process_provider_notification(s, pp.provider_payment_id, gateway)
    assert _fresh(db, Transaction, tx.id).status == TransactionStatus.CONTACT_RELEASED
    assert _count(db, "CONTACT_RELEASED_BIDDER") == 1 and _count(db, "CONTACT_RELEASED_OWNER") == 1
    assert db.execute(select(func.count()).select_from(Payment)).scalar_one() == 1


# ---- system modes -------------------------------------------------------------------------------------------
def _admin_headers(client):
    with dbmod.session_scope() as s:
        create_admin(s, "root", "root-password-123456", AdminRole.ADMIN)
    r = client.post("/admin/auth/login", json={"username": "root", "password": "root-password-123456"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_pause_freezes_deadlines_and_resume_extends_them(db, client, make_user, publish_item, accepted, gateway):
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    old_checkout = link.payment.provider_checkout_id
    o2, b2 = make_user(), make_user()
    item2 = publish_item(o2, title="Silla gamer")
    pending = offers.create_offer(db, b2, item2.public_id, offer_type="MONEY", offered_price="1000").offer
    db.commit()
    due_before = _fresh(db, Transaction, tx.id).payment_due_at
    exp_before = _fresh(db, Offer, pending.id).expires_at
    h = _admin_headers(client)

    r = client.put("/admin/system/mode", json={"mode": "PAUSED", "reason": "mantenimiento"}, headers=h)
    assert r.status_code == 200
    with dbmod.session_scope() as s:  # pretend the pause started 2 hours ago
        cfg.set_internal(s, "system_paused_since", (utcnow() - timedelta(hours=2)).isoformat())
    # While paused, deadline jobs do nothing even for overdue rows.
    jobs = {j.name: j for j in maintenance_jobs(gateway)}
    assert run_job(jobs["expire_pending_offers"], "w").status == JobRunStatus.SKIPPED
    assert run_job(jobs["expire_payment_windows"], "w").status == JobRunStatus.SKIPPED
    assert run_job(jobs["warn_expiring_publications"], "w").status == JobRunStatus.SUCCEEDED

    r = client.put("/admin/system/mode", json={"mode": "ONLINE", "reason": "fin mantenimiento"}, headers=h)
    assert r.status_code == 200
    shift_tx = _fresh(db, Transaction, tx.id).payment_due_at - due_before
    shift_offer = _fresh(db, Offer, pending.id).expires_at - exp_before
    assert timedelta(hours=1, minutes=59) < shift_tx < timedelta(hours=2, minutes=1)
    assert timedelta(hours=1, minutes=59) < shift_offer < timedelta(hours=2, minutes=1)
    ext = db.execute(select(Notification.user_id).where(Notification.kind == "DEADLINE_EXTENDED")).scalars().all()
    assert sorted(ext) == sorted([o2.id, b2.id, bidder.id])
    audit_row = db.execute(select(AuditLog).where(AuditLog.action == "SYSTEM_DEADLINES_EXTENDED")).scalar_one()
    assert audit_row.details["offers_extended"] == 1 and audit_row.details["payments_extended"] == 1
    assert cfg.get_internal(db, "system_paused_since") is None
    # The old checkout link expires before the new deadline: a fresh one is issued.
    again = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    assert again.payment.provider_checkout_id != old_checkout


def test_mode_switch_between_non_online_modes_keeps_original_pause_start(db):
    cfg.set_value(db, "system_mode", "PAUSED")
    first = cfg.get_internal(db, "system_paused_since")
    cfg.set_value(db, "system_mode", "EMERGENCY")
    assert cfg.get_internal(db, "system_paused_since") == first
    cfg.set_value(db, "system_mode", "ONLINE")
    assert cfg.get_internal(db, "system_paused_since") is None
    db.commit()


def test_emergency_defers_release_but_webhooks_and_audit_keep_working(db, accepted, gateway, notifications):
    owner, bidder, item, offer, tx = accepted
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    cfg.set_value(db, "system_mode", SystemMode.EMERGENCY.value)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    with dbmod.session_scope() as s:
        payments.process_provider_notification(s, pp.provider_payment_id, gateway)  # webhook still processed
    assert _fresh(db, Transaction, tx.id).status == TransactionStatus.PAID  # money recorded, release deferred
    assert not notifications(bidder.id, "CONTACT_RELEASED_BIDDER")
    jobs = {j.name: j for j in maintenance_jobs(gateway)}
    assert run_job(jobs["release_deferred"], "w").status == JobRunStatus.SKIPPED
    _overdue(db, tx)  # even overdue, a paid transaction is never cancelled
    cfg.set_value(db, "system_mode", SystemMode.ONLINE.value)
    db.commit()
    assert run_job(jobs["expire_payment_windows"], "w").affected == 0
    assert run_job(jobs["release_deferred"], "w").affected == 1
    assert _fresh(db, Transaction, tx.id).status == TransactionStatus.CONTACT_RELEASED
    assert notifications(bidder.id, "CONTACT_RELEASED_BIDDER") and notifications(owner.id, "CONTACT_RELEASED_OWNER")
