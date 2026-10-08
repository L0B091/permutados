"""Attractiveness score 0-100 (section 26).

Each signal is normalised to 0..1, then combined with fixed weights that sum to 100:

| signal          | weight | source                                                                             |
|-----------------|-------:|------------------------------------------------------------------------------------|
| demand          |      8 | distinct users interested in the item's category (14 days), relative to the top one |
| searches        |     10 | distinct users who saw the item in search results (14 days), log-capped at 50       |
| selections      |     10 | distinct users who selected it (14 days), log-capped at 20                          |
| views           |      7 | distinct users who opened its detail (14 days), log-capped at 20                    |
| offers          |     10 | distinct bidders (30 days), log-capped at 5                                         |
| acceptance      |      5 | owner's smoothed acceptance rate ((accepted+1)/(decided+2))                          |
| recency         |     10 | fades linearly over 14 days since publication                                       |
| price           |      8 | price vs the category median of published items (needs >= 3 comparables)            |
| swap            |      4 | accepts swaps                                                                       |
| image_quality   |     10 | quality score of the photo (0..60 -> 0..1)                                          |
| completeness    |      8 | title, description, category, zone, price, confirmed identification                 |
| zone            |      5 | distinct users interested in the item's zone (14 days), relative to the top zone     |
| time_remaining  |      5 | days left before the 30-day expiry (full at >= 7 days)                              |

Availability is a gate, not a weight: anything that is not PUBLISHED, is expired, has an open report or an owner
who is not ACTIVE scores 0 and is never suggested.

Abuse resistance: demand counters are *distinct users per day* (``item_interactions``), the owner's own activity is
never counted, interactions from suspended/banned users are ignored, counts are log-scaled and capped, a
suspiciously low price (< 20% of the category median, typical bait) gets a low price signal, owners with
anti-evasion strikes get a 20% penalty, and exposure in the channel temporarily lowers the score
(``penalty = 30 * exp(-hours / decay)`` per recent exposure, capped at 60) so the same item does not saturate it.
The AI never touches these numbers.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.channel import ChannelExposure
from app.models.enums import InteractionKind, ItemStatus, OfferStatus, ReportStatus, UserStatus
from app.models.item import Item, ItemInteraction
from app.models.offer import Offer
from app.models.report import Report
from app.models.user import User

WEIGHTS: dict[str, int] = {
    "demand": 8,
    "searches": 10,
    "selections": 10,
    "views": 7,
    "offers": 10,
    "acceptance": 5,
    "recency": 10,
    "price": 8,
    "swap": 4,
    "image_quality": 10,
    "completeness": 8,
    "zone": 5,
    "time_remaining": 5,
}
assert sum(WEIGHTS.values()) == 100

DEMAND_WINDOW = timedelta(days=14)
OFFER_WINDOW = timedelta(days=30)
EXPOSURE_PENALTY_PER = 30.0
EXPOSURE_PENALTY_CAP = 60.0
OWNER_STRIKE_FACTOR = 0.8
ACCEPTED_STATES = (OfferStatus.ACCEPTED, OfferStatus.PAYMENT_PENDING, OfferStatus.PAID, OfferStatus.CONTACT_RELEASED)


def log_cap(value: float, cap: float) -> float:
    """0..1, logarithmic, saturating at ``cap``."""
    if value <= 0 or cap <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(cap))


def price_signal(price: float | None, comparables: list[float]) -> float:
    if not price or len(comparables) < 3:
        return 0.5  # neutral when there is nothing to compare with
    median = statistics.median(comparables)
    if median <= 0:
        return 0.5
    r = price / median
    if r < 0.2:
        return 0.2  # too good to be true: bait / fraud risk, do not promote
    return max(0.0, min(1.0, 1.25 - 0.5 * r))  # 0.5x median -> 1.0, median -> 0.75, 2.5x -> 0


def exposure_penalty(exposed_at: list[datetime], now: datetime, decay_hours: float) -> float:
    p = 0.0
    for t in exposed_at:
        hours = max(0.0, (now - t).total_seconds() / 3600)
        p += EXPOSURE_PENALTY_PER * math.exp(-hours / max(1.0, decay_hours))
    return min(EXPOSURE_PENALTY_CAP, p)


@dataclass
class ScoreResult:
    score: int
    available: bool
    signals: dict[str, float] = field(default_factory=dict)
    base: float = 0.0
    owner_factor: float = 1.0
    exposure_penalty: float = 0.0
    reason: str | None = None

    def breakdown(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "available": self.available,
            "reason": self.reason,
            "base": round(self.base, 2),
            "owner_factor": self.owner_factor,
            "exposure_penalty": round(self.exposure_penalty, 2),
            "signals": {k: round(v, 3) for k, v in self.signals.items()},
            "weights": WEIGHTS,
        }


def _distinct_users(session: Session, kind: InteractionKind, since_day, item_ids: list[int]) -> dict[int, int]:
    rows = session.execute(
        select(ItemInteraction.item_id, func.count(func.distinct(ItemInteraction.user_id)))
        .join(User, User.id == ItemInteraction.user_id)
        .where(
            ItemInteraction.kind == kind,
            ItemInteraction.day >= since_day,
            ItemInteraction.item_id.in_(item_ids),
            User.status == UserStatus.ACTIVE,
        )
        .group_by(ItemInteraction.item_id)
    ).all()
    return dict(rows)


def _group_demand(session: Session, column, since_day) -> dict[Any, int]:
    rows = session.execute(
        select(column, func.count(func.distinct(ItemInteraction.user_id)))
        .join(Item, Item.id == ItemInteraction.item_id)
        .join(User, User.id == ItemInteraction.user_id)
        .where(
            ItemInteraction.day >= since_day,
            ItemInteraction.kind.in_([InteractionKind.IMPRESSION, InteractionKind.SELECTION, InteractionKind.VIEW]),
            User.status == UserStatus.ACTIVE,
            column.is_not(None),
        )
        .group_by(column)
    ).all()
    return dict(rows)


def compute_scores(
    session: Session, items: list[Item], now: datetime, *, decay_hours: float = 72.0
) -> dict[int, ScoreResult]:
    if not items:
        return {}
    ids = [i.id for i in items]
    since_day = (now - DEMAND_WINDOW).date()
    impressions = _distinct_users(session, InteractionKind.IMPRESSION, since_day, ids)
    selections = _distinct_users(session, InteractionKind.SELECTION, since_day, ids)
    views = _distinct_users(session, InteractionKind.VIEW, since_day, ids)
    bidders = _distinct_users(session, InteractionKind.OFFER, (now - OFFER_WINDOW).date(), ids)
    cat_demand = _group_demand(session, Item.category, since_day)
    zone_demand = _group_demand(session, Item.zone_id, since_day)
    top_cat = max(cat_demand.values(), default=0)
    top_zone = max(zone_demand.values(), default=0)

    owners = {i.owner_id for i in items}
    owner_rows = {u.id: u for u in session.execute(select(User).where(User.id.in_(owners))).scalars()}
    decided = dict(
        session.execute(
            select(Offer.owner_id, func.count())
            .where(
                Offer.owner_id.in_(owners),
                Offer.status.in_([*ACCEPTED_STATES, OfferStatus.REJECTED, OfferStatus.EXPIRED]),
            )
            .group_by(Offer.owner_id)
        ).all()
    )
    accepted = dict(
        session.execute(
            select(Offer.owner_id, func.count())
            .where(Offer.owner_id.in_(owners), Offer.status.in_(ACCEPTED_STATES))
            .group_by(Offer.owner_id)
        ).all()
    )
    reported = set(
        session.execute(
            select(Report.item_id).where(
                Report.item_id.in_(ids), Report.status.in_([ReportStatus.OPEN, ReportStatus.REVIEWING])
            )
        ).scalars()
    )
    comparables: dict[str | None, list[float]] = defaultdict(list)
    for cat, price in session.execute(
        select(Item.category, Item.base_price).where(
            Item.status == ItemStatus.PUBLISHED, Item.category.in_({i.category for i in items if i.category})
        )
    ).all():
        if price:
            comparables[cat].append(float(price))
    exposures: dict[int, list[datetime]] = defaultdict(list)
    for item_id, ts in session.execute(
        select(ChannelExposure.item_id, ChannelExposure.exposed_at).where(
            and_(
                ChannelExposure.item_id.in_(ids),
                ChannelExposure.exposed_at > now - timedelta(hours=decay_hours * 5),
            )
        )
    ).all():
        exposures[item_id].append(ts)

    out: dict[int, ScoreResult] = {}
    for it in items:
        owner = owner_rows.get(it.owner_id)
        reason = None
        if it.status != ItemStatus.PUBLISHED:
            reason = f"status {it.status.value}"
        elif not it.expires_at or it.expires_at <= now:
            reason = "expired"
        elif owner is None or owner.status != UserStatus.ACTIVE:
            reason = "owner not active"
        elif it.id in reported:
            reason = "open report"
        if reason:
            out[it.id] = ScoreResult(0, False, reason=reason)
            continue
        age_h = max(0.0, (now - (it.published_at or now)).total_seconds() / 3600)
        remaining_h = (it.expires_at - now).total_seconds() / 3600
        quality = float(it.images[0].quality_score or 0) if it.images else 0.0
        fields = (it.title, it.description, it.category, it.zone_id, it.base_price, it.identification_confirmed)
        comps = [p for p in comparables.get(it.category, [])]
        if it.base_price and float(it.base_price) in comps:
            comps.remove(float(it.base_price))  # compare with the others
        signals = {
            "demand": (cat_demand.get(it.category, 0) / top_cat) if top_cat else 0.0,
            "searches": log_cap(impressions.get(it.id, 0), 50),
            "selections": log_cap(selections.get(it.id, 0), 20),
            "views": log_cap(views.get(it.id, 0), 20),
            "offers": log_cap(bidders.get(it.id, 0), 5),
            "acceptance": (accepted.get(it.owner_id, 0) + 1) / (decided.get(it.owner_id, 0) + 2),
            "recency": max(0.0, 1 - age_h / (24 * 14)),
            "price": price_signal(float(it.base_price) if it.base_price else None, comps),
            "swap": 1.0 if it.accepts_swap else 0.0,
            "image_quality": min(1.0, quality / 60),
            "completeness": sum(bool(x) for x in fields) / len(fields),
            "zone": (zone_demand.get(it.zone_id, 0) / top_zone) if top_zone else 0.0,
            "time_remaining": max(0.0, min(1.0, remaining_h / (24 * 7))),
        }
        base = sum(WEIGHTS[k] * v for k, v in signals.items())
        factor = OWNER_STRIKE_FACTOR if owner.evasion_strikes > 0 else 1.0
        penalty = exposure_penalty(exposures.get(it.id, []), now, decay_hours)
        score = int(max(0, min(100, round(base * factor - penalty))))
        out[it.id] = ScoreResult(score, True, signals, base, factor, penalty)
    return out


def attractiveness(session: Session, item: Item, now: datetime, *, decay_hours: float = 72.0) -> int:
    return compute_scores(session, [item], now, decay_hours=decay_hours)[item.id].score
