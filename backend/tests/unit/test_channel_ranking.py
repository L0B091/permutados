"""Pure parts of the channel engine: signal normalisation, exposure decay, diversity, channel status."""

import math
from datetime import UTC, datetime, timedelta

from app.models.enums import ChannelPostStatus as P
from app.models.enums import ItemChannelStatus as C
from app.services.channel.score import WEIGHTS, exposure_penalty, log_cap, price_signal
from app.services.channel.service import derive_channel_status, diversify

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def test_weights_sum_to_100_and_cover_section_26():
    assert sum(WEIGHTS.values()) == 100
    for k in (
        "demand",
        "searches",
        "selections",
        "views",
        "offers",
        "acceptance",
        "recency",
        "price",
        "swap",
        "image_quality",
        "completeness",
        "zone",
        "time_remaining",
    ):
        assert k in WEIGHTS


def test_log_cap_is_bounded_and_saturates():
    assert log_cap(0, 50) == 0 and log_cap(-3, 50) == 0
    assert 0 < log_cap(1, 50) < log_cap(10, 50) < 1
    assert log_cap(50, 50) == 1 and log_cap(10_000, 50) == 1  # spam cannot push beyond the cap


def test_price_signal_rewards_good_prices_but_flags_bait():
    comps = [100.0, 100.0, 100.0]
    assert price_signal(100, [1.0, 2.0]) == 0.5  # not enough comparables: neutral
    assert price_signal(50, comps) == 1.0
    assert math.isclose(price_signal(100, comps), 0.75)
    assert price_signal(250, comps) == 0.0
    assert price_signal(10, comps) == 0.2  # 10% of the median: too good to be true


def test_exposure_penalty_decays_and_is_capped():
    assert exposure_penalty([], NOW, 72) == 0
    fresh = exposure_penalty([NOW], NOW, 72)
    assert fresh == 30
    assert exposure_penalty([NOW - timedelta(hours=72)], NOW, 72) < 12
    assert exposure_penalty([NOW] * 5, NOW, 72) == 60


def _c(pk, prio, cat, owner, zone=1, bucket=1, swap=True, age=0):
    return {
        "item_pk": pk,
        "priority": prio,
        "category": cat,
        "zone_id": zone,
        "price_bucket": bucket,
        "swap": swap,
        "age_bucket": age,
        "owner_id": owner,
    }


def test_diversify_owner_cap_category_cap_and_no_consecutive_category():
    cands = [
        _c(1, 90, "muebles", 1),
        _c(2, 89, "muebles", 2),
        _c(3, 88, "muebles", 3),
        _c(4, 87, "muebles", 4),
        _c(5, 60, "bicis", 5),
        _c(6, 59, "tecno", 6),
        _c(7, 85, "muebles", 1),
    ]
    out = diversify(cands, 6)
    cats = [c["category"] for c in out]
    assert cats.count("muebles") <= 2  # ceil(6/3)
    assert all(a != b for a, b in zip(cats, cats[1:], strict=False))
    owners = [c["owner_id"] for c in out]
    assert len(owners) == len(set(owners))  # one item per owner
    assert out[0]["item_pk"] == 1  # the best item still leads


def test_diversify_prefers_variety_in_zone_and_price():
    cands = [
        _c(1, 80, "a", 1, zone=1, bucket=1),
        _c(2, 79, "b", 2, zone=1, bucket=1),
        _c(3, 76, "c", 3, zone=2, bucket=2),
    ]
    out = diversify(cands, 2)
    assert [c["item_pk"] for c in out] == [1, 3]
    out = diversify(cands, 2, zone=False, price=False)
    assert [c["item_pk"] for c in out] == [1, 2]


def test_diversify_relaxes_consecutive_rule_only_when_needed():
    cands = [_c(1, 80, "a", 1), _c(2, 70, "a", 2), _c(3, 60, "a", 3)]
    out = diversify(cands, 9)  # cap ceil(9/3)=3: all three allowed, back-to-back as there is nothing else
    assert [c["item_pk"] for c in out] == [1, 2, 3]
    assert len(diversify(cands, 3)) == 1  # cap 1 per category: shorter list rather than monotonous


def test_derive_channel_status():
    assert derive_channel_status([]) == C.NOT_POSTED
    assert derive_channel_status([(P.PENDING, None)]) == C.QUEUED
    assert derive_channel_status([(P.REMOVED, NOW), (P.PUBLISHED, NOW)]) == C.VISIBLE
    assert derive_channel_status([(P.REMOVAL_PENDING, NOW)]) == C.REMOVAL_PENDING
    assert derive_channel_status([(P.REMOVED, NOW)]) == C.REMOVED
    assert derive_channel_status([(P.FAILED, None)]) == C.NOT_POSTED
