"""Attractiveness score 0-100 (section 26). Normalised, bounded per signal and resistant to abuse:
counters are log-scaled and capped so a single user spamming searches/selections cannot dominate."""

from __future__ import annotations

import math
from datetime import datetime

from app.models.enums import ItemStatus
from app.models.item import Item


def _log_cap(value: int, cap: int) -> float:
    """0..1, logarithmic, saturating at ``cap``."""
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(cap))


def attractiveness(item: Item, now: datetime, *, recent_exposures: int = 0) -> int:
    if item.status != ItemStatus.PUBLISHED or not item.expires_at or item.expires_at <= now:
        return 0  # closed / expired / reserved items are never promoted
    s = 0.0
    s += 15 * _log_cap(item.search_impressions, 200)  # demand / searches
    s += 15 * _log_cap(item.selection_count, 50)  # selections / views
    s += 10 * _log_cap(item.offer_count, 10)  # offers received
    age_h = max(0.0, (now - (item.published_at or now)).total_seconds() / 3600)
    s += 15 * max(0.0, 1 - age_h / (24 * 14))  # recency (fades over 14 days)
    s += 5 if item.accepts_swap else 0  # swap friendly
    quality = float(item.images[0].quality_score or 0) if item.images else 0.0
    s += 15 * min(1.0, quality / 60)  # image quality
    completeness = sum(bool(x) for x in (item.title, item.description, item.category, item.zone_id, item.base_price))
    s += 10 * completeness / 5  # completeness
    remaining_h = (item.expires_at - now).total_seconds() / 3600
    s += 5 if remaining_h > 72 else 0  # time left
    s += 10 if not item.has_damage and item.works else 0  # condition
    s -= 12 * min(recent_exposures, 3)  # temporary decay after exposure
    return int(max(0, min(100, round(s))))
