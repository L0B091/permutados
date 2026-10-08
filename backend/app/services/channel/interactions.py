"""Distinct-user interaction counters (abuse-resistant inputs for the attractiveness score).

One row per (item, user, kind, day): a user who searches the same thing 500 times in a day counts once.
The owner's own interactions are never recorded. Raw counters on ``items`` are kept for the admin.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.enums import InteractionKind
from app.models.item import Item, ItemInteraction
from app.services.notifications.templates import AR_TZ


def record(session: Session, user_id: int, item_ids: Iterable[int], kind: InteractionKind) -> int:
    ids = sorted(set(item_ids))
    if not ids:
        return 0
    not_owned = session.execute(select(Item.id).where(Item.id.in_(ids), Item.owner_id != user_id)).scalars().all()
    if not not_owned:
        return 0
    day = utcnow().astimezone(AR_TZ).date()
    stmt = insert(ItemInteraction).values(
        [{"item_id": i, "user_id": user_id, "kind": kind, "day": day, "count": 1} for i in not_owned]
    )
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["item_id", "user_id", "kind", "day"], set_={"count": ItemInteraction.count + 1}
        )
    )
    return len(not_owned)
