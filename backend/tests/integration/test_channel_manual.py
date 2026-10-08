"""Manually administered WhatsApp Channel: attractiveness, diversity, exposure control, admin workflow,
channel_status and reconciliation (real PostgreSQL)."""

from datetime import timedelta

import pytest
from sqlalchemy import delete, func, select, text, update

from app.core.clock import utcnow
from app.core.errors import Conflict, InvalidState
from app.models.channel import ChannelExposure, ChannelPost
from app.models.enums import (
    AdminRole,
    ChannelPostStatus,
    InteractionKind,
    ItemChannelStatus,
    ItemStatus,
    ReportReason,
    ReportStatus,
    UserStatus,
)
from app.models.item import Item, ItemInteraction
from app.models.report import Report
from app.models.user import User
from app.services.admin.auth import create_admin
from app.services.channel import interactions
from app.services.channel import service as channel
from app.services.channel.score import compute_scores
from app.services.offers import service as offers
from app.services.publications import service as pubs
from app.services.system import config_service as cfg


@pytest.fixture
def admin(db):
    a = create_admin(db, "root", "root-password-123456", AdminRole.ADMIN)
    db.commit()
    return a


@pytest.fixture
def channel_on(db):
    cfg.set_value(db, "channel_enabled", True)
    cfg.set_value(db, "channel_min_score", 0)
    db.commit()


def _scores(db, *items):
    db.expire_all()
    fresh = [db.get(Item, i.id) for i in items]
    return compute_scores(db, fresh, utcnow())


def _fresh(db, model, pk):
    db.expire_all()
    return db.get(model, pk)


# ---- interactions & score ------------------------------------------------------------------------------------
def test_interactions_are_distinct_per_user_and_day_and_exclude_owner(db, make_user, publish_item):
    owner, viewer = make_user(), make_user()
    item = publish_item(owner)
    for _ in range(20):
        interactions.record(db, viewer.id, [item.id], InteractionKind.IMPRESSION)
    assert interactions.record(db, owner.id, [item.id], InteractionKind.VIEW) == 0
    db.commit()
    rows = db.execute(select(ItemInteraction)).scalars().all()
    assert len(rows) == 1 and rows[0].count == 20 and rows[0].user_id == viewer.id


def test_real_search_records_impressions(db, make_user, publish_item):
    from app.services.search import service as search

    owner, buyer = make_user(), make_user()
    item = publish_item(owner, title="Bicicleta rodado 29")
    search.start_search(db, buyer, "bicicleta")
    search.start_search(db, owner, "bicicleta")  # the owner's own searches never count
    db.commit()
    rows = db.execute(select(ItemInteraction.user_id, ItemInteraction.kind)).all()
    assert rows == [(buyer.id, InteractionKind.IMPRESSION)]
    assert _scores(db, item)[item.id].signals["searches"] > 0


def test_spam_from_one_user_counts_once_and_inactive_users_are_ignored(db, make_user, publish_item):
    a_owner, b_owner = make_user(), make_user()
    a = publish_item(a_owner, title="Mesa de roble")
    b = publish_item(b_owner, title="Mesa ratona")
    spammer = make_user()
    for _ in range(50):
        interactions.record(db, spammer.id, [a.id], InteractionKind.SELECTION)
    fans = [make_user() for _ in range(5)]
    for f in fans:
        interactions.record(db, f.id, [b.id], InteractionKind.SELECTION)
    db.commit()
    s = _scores(db, a, b)
    assert s[a.id].signals["selections"] < s[b.id].signals["selections"]
    assert s[a.id].score < s[b.id].score
    db.execute(update(User).where(User.id.in_([f.id for f in fans])).values(status=UserStatus.SUSPENDED))
    db.commit()
    assert _scores(db, b)[b.id].signals["selections"] == 0


def test_availability_gate_and_penalties(db, make_user, publish_item, admin):
    owner = make_user()
    good = publish_item(owner, title="Heladera", category="electro", price="100000")
    others = [publish_item(make_user(), title=f"Heladera {n}", category="electro", price="100000") for n in range(3)]
    bait = publish_item(make_user(), title="Heladera regalo", category="electro", price="5000")
    s = _scores(db, good, bait, *others)
    assert all(0 <= r.score <= 100 for r in s.values())
    assert s[bait.id].signals["price"] == 0.2  # suspiciously cheap: not promoted
    assert s[good.id].available and s[good.id].owner_factor == 1.0
    base = s[good.id].score

    db.add(Report(reporter_id=others[0].owner_id, item_id=good.id, reason=ReportReason.FRAUD))
    db.commit()
    r = _scores(db, good)[good.id]
    assert r.score == 0 and not r.available and r.reason == "open report"
    db.execute(update(Report).values(status=ReportStatus.DISMISSED))
    db.execute(update(User).where(User.id == owner.id).values(evasion_strikes=1))
    db.commit()
    r = _scores(db, good)[good.id]
    assert r.owner_factor == 0.8 and r.score < base
    db.execute(update(User).where(User.id == owner.id).values(status=UserStatus.SUSPENDED))
    db.commit()
    assert _scores(db, good)[good.id].reason == "owner not active"


def test_exposure_lowers_score_temporarily(db, make_user, publish_item, admin):
    item = publish_item(make_user())
    before = _scores(db, item)[item.id].score
    post = channel.register_manual_post(db, admin.id, item.public_id, external_ref=None)
    db.commit()
    after = _scores(db, item)[item.id]
    assert 29.9 < after.exposure_penalty <= 30 and before - 31 <= after.score <= before - 29
    db.execute(update(ChannelExposure).values(exposed_at=utcnow() - timedelta(days=10)))
    db.commit()
    assert _scores(db, item)[item.id].exposure_penalty < 2  # 30*exp(-240/72) ~ 1.07
    assert post.id


def test_rejected_or_reserved_items_are_not_available(db, make_user, publish_item):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1")
    db.commit()
    r = _scores(db, item)[item.id]
    assert r.score == 0 and r.reason == "status RESERVED"


# ---- suggestions -------------------------------------------------------------------------------------------
def test_suggestions_respect_switch_quota_exposure_and_featured_rules(db, make_user, publish_item, admin):
    rep = channel.suggestion_report(db)
    assert rep["reason"] == "channel_disabled" and rep["items"] == []
    cfg.set_value(db, "channel_enabled", True)
    cfg.set_value(db, "channel_min_score", 0)
    cfg.set_value(db, "channel_max_exposure_per_item", 1)
    db.commit()
    cats = ["muebles", "bicicletas", "electro", "tecnologia"]
    items = [publish_item(make_user(), title=f"Cosa {n}", category=cats[n % 4]) for n in range(6)]
    rep = channel.suggestion_report(db, limit=10)
    assert len(rep["items"]) == 6 and rep["candidates"] == 6
    first = rep["items"][0]
    assert set(first["score_breakdown"]["signals"]) == set(first["score_breakdown"]["weights"])

    # Posted once -> live post (not suggested); removed -> max exposure (1) reached.
    post = channel.register_manual_post(db, admin.id, items[0].public_id, external_ref=None)
    rep = channel.suggestion_report(db, limit=10)
    assert rep["skipped"]["live_post"] == 1
    channel.confirm_removed(db, admin.id, post.id)
    rep = channel.suggestion_report(db, limit=10)
    assert rep["skipped"]["max_exposure"] == 1
    cfg.set_value(db, "channel_max_exposure_per_item", 5)
    rep = channel.suggestion_report(db, limit=10)
    assert rep["skipped"]["too_recent"] == 1  # 48h between repeats of the same item

    # Featured: bonus in the ranking, but never below the minimum score.
    low = items[5]
    channel.set_featured(db, admin.id, low.public_id, True)
    db.commit()
    rep = channel.suggestion_report(db, limit=10)
    feat = next(i for i in rep["items"] if i["item_id"] == low.public_id)
    assert feat["featured"] and feat["priority"] == feat["score"] + channel.FEATURED_BONUS
    cfg.set_value(db, "channel_min_score", feat["score"] + 1)
    rep = channel.suggestion_report(db, limit=10)
    assert low.public_id not in [i["item_id"] for i in rep["items"]]

    # Daily quota: posts in the last 24h + queued count against the limit.
    cfg.set_value(db, "channel_min_score", 0)
    cfg.set_value(db, "channel_daily_limit", 2)
    db.commit()
    rep = channel.suggestion_report(db, limit=10)
    assert len(rep["items"]) == 1 and rep["quota"]["remaining"] == 1
    channel.queue_posts(db, admin.id, [rep["items"][0]["item_id"]])
    rep = channel.suggestion_report(db, limit=10)
    assert rep["reason"] == "daily_limit_reached"
    with pytest.raises(Conflict):
        channel.queue_posts(db, admin.id, [items[1].public_id])
    db.rollback()


def test_suggestions_are_diverse(db, make_user, publish_item, channel_on):
    for n in range(5):
        publish_item(make_user(), title=f"Silla {n}", category="muebles", zone="Palermo")
    publish_item(make_user(), title="Bici", category="bicicletas", zone="Caballito")
    publish_item(make_user(), title="Notebook", category="tecnologia", zone="Belgrano")
    out = channel.suggestions(db, limit=6)
    cats = [i["category"] for i in out]
    assert cats.count("muebles") <= 2
    assert all(a != b for a, b in zip(cats, cats[1:], strict=False))
    assert {"bicicletas", "tecnologia"} <= set(cats)


# ---- admin workflow ---------------------------------------------------------------------------------------
def test_queue_publish_fail_retry_remove_flow(db, make_user, publish_item, admin, channel_on):
    a = publish_item(make_user(), title="Mesa", category="muebles")
    b = publish_item(make_user(), title="Bici", category="bicicletas")
    pa, pb = channel.queue_posts(db, admin.id, [a.public_id, b.public_id])
    db.commit()
    assert _fresh(db, Item, a.id).channel_status == ItemChannelStatus.QUEUED
    with pytest.raises(Conflict):
        channel.queue_posts(db, admin.id, [a.public_id])  # one live post per item
    db.rollback()

    channel.confirm_published(db, admin.id, pa.id, external_ref="https://whatsapp.com/channel/x/1", position=1)
    channel.confirm_published(db, admin.id, pa.id, external_ref="dup")  # idempotent
    db.commit()
    assert _fresh(db, Item, a.id).channel_status == ItemChannelStatus.VISIBLE
    exp = db.execute(select(ChannelExposure).where(ChannelExposure.item_id == a.id)).scalars().all()
    assert len(exp) == 1 and exp[0].position == 1

    channel.mark_failed(db, admin.id, pb.id, "WhatsApp web no cargó")
    db.commit()
    assert _fresh(db, Item, b.id).channel_status == ItemChannelStatus.NOT_POSTED
    channel.retry_post(db, admin.id, pb.id)
    channel.confirm_published(db, admin.id, pb.id, external_ref=None)
    db.commit()
    assert _fresh(db, ChannelPost, pb.id).status == ChannelPostStatus.PUBLISHED

    # Taking a post down never changes the publication itself (channel_status is separate).
    channel.request_removal(db, admin.id, pa.id, "ROTATION")
    db.commit()
    it = _fresh(db, Item, a.id)
    assert it.channel_status == ItemChannelStatus.REMOVAL_PENDING and it.status == ItemStatus.PUBLISHED
    channel.confirm_removed(db, admin.id, pa.id)
    db.commit()
    it = _fresh(db, Item, a.id)
    assert it.channel_status == ItemChannelStatus.REMOVED and it.status == ItemStatus.PUBLISHED
    with pytest.raises(InvalidState):
        channel.mark_failed(db, admin.id, pa.id, "x")
    db.rollback()


def test_item_closed_while_queued_then_admin_reports_it_was_posted(db, make_user, publish_item, admin, channel_on):
    owner = make_user()
    item = publish_item(owner)
    [post] = channel.queue_posts(db, admin.id, [item.public_id])
    pubs.delete_publication(db, owner, item.id)
    db.commit()
    p = _fresh(db, ChannelPost, post.id)
    assert p.status == ChannelPostStatus.REMOVED and p.posted_at is None  # never visible: just cancelled
    assert channel.removals_needed(db)["removal_pending"] == []
    # ...but the admin had already posted it by hand: reality wins, it must now be removed.
    channel.confirm_published(db, admin.id, post.id, external_ref="https://whatsapp.com/channel/x/9")
    db.commit()
    p = _fresh(db, ChannelPost, post.id)
    assert p.status == ChannelPostStatus.REMOVAL_PENDING and p.posted_at is not None
    assert [r["id"] for r in channel.removals_needed(db)["removal_pending"]] == [post.id]


def test_suspended_owner_posts_flagged_immediately_and_cannot_be_posted(db, make_user, publish_item, admin):
    from app.services.security.enforcement import suspend_pending_review

    owner = make_user()
    a, b = publish_item(owner, title="Mesa"), publish_item(owner, title="Silla")
    post = channel.register_manual_post(db, admin.id, a.public_id, external_ref=None)
    db.commit()
    suspend_pending_review(db, owner.id, reason="EVASION_REPEATED")
    db.commit()
    assert _fresh(db, ChannelPost, post.id).status == ChannelPostStatus.REMOVAL_PENDING
    assert _fresh(db, ChannelPost, post.id).removal_reason == "OWNER_BLOCKED"
    with pytest.raises(InvalidState):
        channel.register_manual_post(db, admin.id, b.public_id, external_ref=None)
    db.rollback()


# ---- reconciliation -----------------------------------------------------------------------------------------
def test_reconciliation_fixes_and_reports(db, make_user, publish_item, admin, channel_on):
    closed = publish_item(make_user(), title="Cerrado", category="a")
    blocked_owner = make_user()
    blocked = publish_item(blocked_owner, title="Bloqueado", category="b")
    stale = publish_item(make_user(), title="Encolado", category="c")
    noexp = publish_item(make_user(), title="Sin exposición", category="d")
    paused_owner, bidder = make_user(), make_user()
    paused = publish_item(paused_owner, title="Pausado", category="e")
    p_closed = channel.register_manual_post(db, admin.id, closed.public_id, external_ref=None)
    p_blocked = channel.register_manual_post(db, admin.id, blocked.public_id, external_ref=None)
    [p_stale] = channel.queue_posts(db, admin.id, [stale.public_id])
    p_noexp = channel.register_manual_post(db, admin.id, noexp.public_id, external_ref=None)
    p_paused = channel.register_manual_post(db, admin.id, paused.public_id, external_ref="https://x/1")
    db.commit()
    offers.create_offer(db, bidder, paused.public_id, offer_type="MONEY", offered_price="1")
    # Inconsistencies a missed event / crash / manual DB edit could leave behind:
    db.execute(update(Item).where(Item.id == closed.id).values(status=ItemStatus.COMPLETED))
    db.execute(update(User).where(User.id == blocked_owner.id).values(status=UserStatus.BANNED))
    db.execute(
        text("UPDATE channel_posts SET created_at = now() - interval '25 hours' WHERE id = :i"), {"i": p_stale.id}
    )
    db.execute(delete(ChannelExposure).where(ChannelExposure.channel_post_id == p_noexp.id))
    db.execute(update(Item).where(Item.id == noexp.id).values(channel_status=ItemChannelStatus.NOT_POSTED))
    db.commit()

    rep = channel.reconcile(db)
    db.commit()
    f = rep["fixed"]
    assert f["closed_items_flagged"] == 1 and f["blocked_owner_flagged"] == 1
    assert f["stale_queue_failed"] == 1 and f["missing_exposures_created"] == 1
    assert f["channel_status_corrected"] >= 1
    assert _fresh(db, ChannelPost, p_closed.id).status == ChannelPostStatus.REMOVAL_PENDING
    assert _fresh(db, ChannelPost, p_blocked.id).status == ChannelPostStatus.REMOVAL_PENDING
    assert _fresh(db, ChannelPost, p_stale.id).status == ChannelPostStatus.FAILED
    assert _fresh(db, Item, noexp.id).channel_status == ItemChannelStatus.VISIBLE
    assert rep["issues"]["visible_but_paused"] == [{"post_id": p_paused.id, "item_id": paused.public_id}]
    assert rep["issues"]["failed_posts"][0]["post_id"] == p_stale.id
    assert rep["removal_pending"] == 2

    # Second pass: everything consistent, nothing to fix (idempotent).
    rep2 = channel.reconcile(db)
    assert rep2["affected"] == 0
    # Removals that wait too long are escalated in the report.
    db.execute(
        update(ChannelPost)
        .where(ChannelPost.status == ChannelPostStatus.REMOVAL_PENDING)
        .values(removal_requested_at=utcnow() - timedelta(hours=7))
    )
    db.commit()
    assert len(channel.reconcile(db)["issues"]["removal_overdue"]) == 2
    rem = channel.removals_needed(db)
    assert len(rem["removal_pending"]) == 2 and all(r["overdue"] for r in rem["removal_pending"])
    assert db.execute(select(func.count()).select_from(ChannelPost)).scalar_one() == 5
