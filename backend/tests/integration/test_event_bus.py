"""Internal event bus (section 35): persisted events, best-effort vs critical handlers, retries, and the
channel reacting immediately to closing events (real PostgreSQL)."""

from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.core.clock import utcnow
from app.models.audit import DomainEvent
from app.models.channel import ChannelPost
from app.models.enums import AdminRole, ChannelPostStatus, ItemChannelStatus, ItemStatus
from app.models.item import Item
from app.services.admin.auth import create_admin
from app.services.channel import service as channel
from app.services.events.bus import EventType, get_bus, handler_name
from app.services.offers import service as offers
from app.services.payments import service as payments
from app.services.publications import service as pubs


@pytest.fixture
def admin(db):
    a = create_admin(db, "root", "root-password-123456", AdminRole.ADMIN)
    db.commit()
    return a


@pytest.fixture
def extra_handlers():
    bus = get_bus()
    added: list[tuple[str, object]] = []

    def _add(event_type, handler, critical=False):
        bus.subscribe(event_type, handler, critical=critical)
        added.append((str(event_type), handler))

    yield _add
    for et, h in added:
        bus._subs[et] = [s for s in bus._subs[et] if s.handler is not h]


def _events(db, aggregate_id=None):
    db.expire_all()
    stmt = select(DomainEvent).order_by(DomainEvent.id)
    if aggregate_id:
        stmt = stmt.where(DomainEvent.aggregate_id == aggregate_id)
    return list(db.execute(stmt).scalars())


def test_lifecycle_emits_specific_events(db, make_user, publish_item, gateway):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    pubs.update_publication(db, owner, item.id, {"base_price": "160000"})
    o1 = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1").offer
    offers.reject_offer(db, owner, o1.public_id)
    o2 = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="2").offer
    offers.cancel_offer(db, bidder, o2.public_id)
    offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="3")
    db.commit()
    offers.expire_pending_offers(db, now=utcnow() + timedelta(hours=25))
    o4 = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="4").offer
    tx = offers.accept_offer(db, owner, o4.public_id).transaction
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    payments.process_provider_notification(db, pp.provider_payment_id, gateway)
    db.commit()
    types = [e.event_type for e in _events(db)]
    for et in (
        "ITEM_CREATED",
        "ITEM_UPDATED",
        "OFFER_CREATED",
        "OFFER_REJECTED",
        "OFFER_CANCELLED",
        "OFFER_EXPIRED",
        "OFFER_ACCEPTED",
        "PAYMENT_CONFIRMED",
        "CONTACT_RELEASED",
        "ITEM_COMPLETED",
    ):
        assert et in types, et
    assert types.index("PAYMENT_CONFIRMED") < types.index("CONTACT_RELEASED")
    assert all(e.processed_at is not None and e.failed_handlers == [] for e in _events(db))
    assert "OFFER_CLOSED" not in types  # replaced by the specific OFFER_* events


@pytest.mark.parametrize("closing", ["COMPLETED", "EXPIRED", "DELETED"])
def test_channel_flags_removal_immediately_on_close(db, make_user, publish_item, admin, gateway, closing):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    post = channel.register_manual_post(db, admin.id, item.public_id, external_ref="https://wa.me/c/1")
    db.commit()
    assert db.get(Item, item.id).channel_status == ItemChannelStatus.VISIBLE
    if closing == "COMPLETED":
        o = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="5").offer
        tx = offers.accept_offer(db, owner, o.public_id).transaction
        link = payments.create_payment(db, bidder, tx.public_id, gateway)
        db.commit()
        pp = gateway.simulate_payment(link.payment.external_reference)
        payments.process_provider_notification(db, pp.provider_payment_id, gateway)
    elif closing == "EXPIRED":
        from app.workers.jobs import expire_publications

        expire_publications(db, now=utcnow() + timedelta(days=31))
    else:
        pubs.delete_publication(db, owner, item.id)
    # Same transaction, no polling: the post is already flagged when the closing operation commits.
    db.commit()
    db.expire_all()
    it = db.get(Item, item.id)
    assert it.status == ItemStatus[closing]
    assert db.get(ChannelPost, post.id).status == ChannelPostStatus.REMOVAL_PENDING
    assert it.channel_status == ItemChannelStatus.REMOVAL_PENDING
    assert [r["id"] for r in channel.removals_needed(db)["removal_pending"]] == [post.id]


def test_best_effort_handler_failure_does_not_block_and_is_retried(db, make_user, publish_item, admin, extra_handlers):
    state = {"fail": True, "calls": 0}

    def flaky(session, event):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("downstream unavailable")

    extra_handlers(EventType.ITEM_DELETED, flaky)
    owner = make_user()
    item = publish_item(owner)
    post = channel.register_manual_post(db, admin.id, item.public_id, external_ref=None)
    pubs.delete_publication(db, owner, item.id)
    db.commit()
    db.expire_all()
    assert db.get(Item, item.id).status == ItemStatus.DELETED  # the owner's operation succeeded
    assert db.get(ChannelPost, post.id).status == ChannelPostStatus.REMOVAL_PENDING  # other handlers ran
    [ev] = [e for e in _events(db, item.public_id) if e.event_type == "ITEM_DELETED"]
    assert ev.processed_at is None and ev.failed_handlers == [handler_name(flaky)]
    assert "RuntimeError" in ev.last_error

    bus = get_bus()
    assert bus.retry_failed(db) == 0  # backoff: too early
    assert bus.retry_failed(db, now=utcnow() + timedelta(minutes=5)) == 0  # still failing
    db.commit()
    state["fail"] = False
    assert bus.retry_failed(db, now=utcnow() + timedelta(hours=1)) == 1
    db.commit()
    [ev] = [e for e in _events(db, item.public_id) if e.event_type == "ITEM_DELETED"]
    assert ev.processed_at is not None and ev.failed_handlers == [] and ev.attempts == 3
    assert state["calls"] == 3


def test_channel_handler_failure_is_recovered_by_retry_job(db, make_user, publish_item, admin, monkeypatch):
    from app.workers.jobs import maintenance_jobs
    from app.workers.runtime import run_job

    owner = make_user()
    item = publish_item(owner)
    post = channel.register_manual_post(db, admin.id, item.public_id, external_ref=None)
    db.commit()
    real = channel.flag_item_posts_for_removal

    def broken(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(channel, "flag_item_posts_for_removal", broken)
    pubs.delete_publication(db, owner, item.id)
    db.commit()
    db.expire_all()
    assert db.get(Item, item.id).status == ItemStatus.DELETED
    assert db.get(ChannelPost, post.id).status == ChannelPostStatus.PUBLISHED  # handler failed
    monkeypatch.setattr(channel, "flag_item_posts_for_removal", real)
    db.execute(text("UPDATE domain_events SET created_at = now() - interval '1 hour'"))
    db.commit()
    job = next(j for j in maintenance_jobs() if j.name == "retry_event_handlers")
    assert run_job(job, "w").affected == 1
    db.expire_all()
    assert db.get(ChannelPost, post.id).status == ChannelPostStatus.REMOVAL_PENDING


def test_critical_handler_failure_rolls_back_the_operation(db, make_user, publish_item, extra_handlers):
    def must_succeed(session, event):
        raise RuntimeError("invariant violated")

    extra_handlers(EventType.ITEM_UPDATED, must_succeed, critical=True)
    owner = make_user()
    item = publish_item(owner)
    with pytest.raises(RuntimeError):
        pubs.update_publication(db, owner, item.id, {"base_price": "999"})
    db.rollback()
    assert str(db.get(Item, item.id).base_price) == "150000.00"
    assert not [e for e in _events(db, item.public_id) if e.event_type == "ITEM_UPDATED"]
