from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.clock import utcnow
from app.core.errors import Conflict, InvalidState
from app.models.audit import AuditLog
from app.models.channel import ChannelPost
from app.models.enums import AdminRole, ChannelPostStatus, ItemStatus
from app.services.admin.auth import create_admin
from app.services.channel import service as channel
from app.services.offers import service as offers
from app.services.publications import service as pubs
from app.workers import jobs


@pytest.fixture
def admin(db):
    a = create_admin(db, "channel-admin", "channel-admin-password", AdminRole.ADMIN)
    db.commit()
    return a


def test_manual_post_and_auto_removal_flag_on_close(db, make_user, publish_item, admin):
    owner = make_user()
    item = publish_item(owner)
    post = channel.register_manual_post(db, admin.id, item.public_id, external_ref="https://whatsapp.com/channel/x")
    db.commit()
    with pytest.raises(Conflict):
        channel.register_manual_post(db, admin.id, item.public_id, external_ref=None)
    pubs.delete_publication(db, owner, item.id)  # ITEM_DELETED event -> channel service
    db.commit()
    db.refresh(post)
    assert post.status == ChannelPostStatus.REMOVAL_PENDING and post.removal_reason == "ITEM_DELETED"
    channel.confirm_removed(db, admin.id, post.id)
    db.commit()
    assert post.status == ChannelPostStatus.REMOVED


def test_cannot_post_unavailable_item(db, make_user, publish_item, admin):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1")
    with pytest.raises(InvalidState):
        channel.register_manual_post(db, admin.id, item.public_id, external_ref=None)


def test_reconciliation_flags_posts_of_closed_items(db, make_user, publish_item, admin):
    owner = make_user()
    item = publish_item(owner)
    post = channel.register_manual_post(db, admin.id, item.public_id, external_ref=None)
    db.commit()
    # Simulate a missed event (e.g. crash between steps): item closed directly in DB.
    db.execute(text("UPDATE items SET status='EXPIRED' WHERE id=:i"), {"i": item.id})
    db.commit()
    assert channel.reconcile(db)["flagged"] == 1
    db.commit()
    db.refresh(post)
    assert post.status == ChannelPostStatus.REMOVAL_PENDING and post.removal_reason == "RECONCILIATION"


def test_suggestions_only_available_and_diverse(db, make_user, publish_item, admin):
    owner, bidder = make_user(), make_user()
    for i in range(6):
        publish_item(owner, title=f"Silla {i}", category="muebles")
    for i in range(3):
        publish_item(owner, title=f"Celular {i}", category="celulares")
    reserved = publish_item(owner, title="Reservado", category="otros")
    offers.create_offer(db, bidder, reserved.public_id, offer_type="MONEY", offered_price="1")
    from app.services.system import config_service as cfg

    cfg.set_value(db, "channel_min_score", 0)
    db.commit()
    s = channel.suggestions(db, limit=6)
    codes = [x["item_id"] for x in s]
    assert reserved.public_id not in codes
    cats = [x["category"] for x in s]
    assert cats.count("muebles") <= 2 and "celulares" in cats


def test_publication_expiry_job_and_channel_removal(db, make_user, publish_item, admin, notifications):
    owner = make_user()
    item = publish_item(owner)
    post = channel.register_manual_post(db, admin.id, item.public_id, external_ref=None)
    db.commit()
    assert jobs.warn_expiring_publications(db, now=utcnow() + timedelta(days=28)) == 1
    assert jobs.expire_publications(db, now=utcnow() + timedelta(days=29)) == 0
    assert jobs.expire_publications(db, now=utcnow() + timedelta(days=30, minutes=1)) == 1
    db.commit()
    db.refresh(item)
    db.refresh(post)
    assert item.status == ItemStatus.EXPIRED and post.status == ChannelPostStatus.REMOVAL_PENDING
    assert notifications(owner.id, "PUBLICATION_EXPIRED") and notifications(owner.id, "PUBLICATION_EXPIRY_WARNING")


def test_jobs_run_with_advisory_lock_and_are_idempotent(make_user, publish_item, db):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1")
    db.execute(text("UPDATE offers SET created_at = now() - interval '2 days', expires_at = now() - interval '1 day'"))
    db.commit()
    defs = jobs.job_definitions()
    fn, _ = defs["expire_pending_offers"]
    assert jobs.run_with_lock("expire_pending_offers", fn) == 1
    assert jobs.run_with_lock("expire_pending_offers", fn) == 0  # recovery re-run is a no-op


def test_audit_log_is_append_only(db, make_user):
    make_user()
    row = db.execute(select(AuditLog)).scalars().first()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("UPDATE audit_logs SET action='X' WHERE id=:i"), {"i": row.id})
    db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("DELETE FROM audit_logs WHERE id=:i"), {"i": row.id})


def test_channel_post_db_uniqueness(db, make_user, publish_item, admin):
    from sqlalchemy.exc import IntegrityError

    owner = make_user()
    item = publish_item(owner)
    channel.register_manual_post(db, admin.id, item.public_id, external_ref=None)
    db.commit()
    db.add(ChannelPost(item_id=item.id, status=ChannelPostStatus.PUBLISHED))
    with pytest.raises(IntegrityError):
        db.flush()
