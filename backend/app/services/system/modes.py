"""System modes across the backend and the workers (section 46).

ONLINE     everything runs.
PAUSED     no new critical operations (offers, acceptances, payment links) and no contact releases. Publishing,
           search, webhooks, payment recording/reconciliation, audit and maintenance keep running.
EMERGENCY  every critical / state-changing user operation is blocked (also publish/edit); sensitive
           (contact-release) messages are held in the outbox. Webhooks, recovery, audit and safe maintenance
           (expiring publications, channel removal flags, reconciliation) keep running.

Deadlines are frozen while the system is not ONLINE: the user cannot act (accept / pay) during a pause, so the
expiry jobs for offers and payment windows do not run, and when the system goes back ONLINE every pending offer
and payment window is extended by the paused duration and both parties are told the new deadline.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.enums import ActorType, OfferStatus, SystemMode, TransactionStatus
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.services.audit.service import AuditAction, audit
from app.services.notifications import templates
from app.services.notifications.service import enqueue
from app.services.system import config_service as cfg

log = logging.getLogger(__name__)

ALL = frozenset(SystemMode)
ONLINE_ONLY = frozenset({SystemMode.ONLINE})
NOT_EMERGENCY = frozenset({SystemMode.ONLINE, SystemMode.PAUSED})


def on_mode_change(session: Session, before: SystemMode, after: SystemMode, *, admin_id: int | None = None) -> dict:
    now = utcnow()
    if before == after:
        return {}
    if before == SystemMode.ONLINE:
        cfg.set_internal(session, "system_paused_since", now.isoformat())
        return {"paused_since": now.isoformat()}
    if after == SystemMode.ONLINE:
        raw = cfg.get_internal(session, "system_paused_since")
        cfg.set_internal(session, "system_paused_since", None)
        if not raw:
            return {}
        return extend_deadlines(session, datetime.fromisoformat(raw), now, admin_id=admin_id)
    return {}


def extend_deadlines(session: Session, paused_since: datetime, now: datetime, *, admin_id: int | None = None) -> dict:
    """Shift every deadline that was running during the pause by the paused duration."""
    delta = now - paused_since
    if delta.total_seconds() <= 0:
        return {}
    offers = (
        session.execute(
            select(Offer).where(Offer.status == OfferStatus.PENDING, Offer.expires_at > paused_since).with_for_update()
        )
        .scalars()
        .all()
    )
    for o in offers:
        o.expires_at = o.expires_at + delta
        item = session.get(Item, o.item_id)
        code, title = (item.public_id, item.title or "") if item else ("", "")
        stamp = int(o.expires_at.timestamp())
        enqueue(
            session,
            user_id=o.owner_id,
            kind="DEADLINE_EXTENDED",
            body=templates.deadline_extended_offer_owner(item_code=code, title=title, expires_at=o.expires_at),
            idempotency_key=f"offer:{o.public_id}:extended:{stamp}:owner",
            expires_at=o.expires_at,
        )
        enqueue(
            session,
            user_id=o.bidder_id,
            kind="DEADLINE_EXTENDED",
            body=templates.deadline_extended_offer_bidder(item_code=code, title=title, expires_at=o.expires_at),
            idempotency_key=f"offer:{o.public_id}:extended:{stamp}:bidder",
            expires_at=o.expires_at,
        )
    txs = (
        session.execute(
            select(Transaction)
            .where(Transaction.status == TransactionStatus.PAYMENT_PENDING, Transaction.payment_due_at > paused_since)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    for tx in txs:
        tx.payment_due_at = tx.payment_due_at + delta
        item = session.get(Item, tx.item_id)
        enqueue(
            session,
            user_id=tx.bidder_id,
            kind="DEADLINE_EXTENDED",
            body=templates.deadline_extended_payment_bidder(
                item_code=item.public_id if item else "",
                title=(item.title or "") if item else "",
                due_at=tx.payment_due_at,
            ),
            idempotency_key=f"tx:{tx.public_id}:extended:{int(tx.payment_due_at.timestamp())}",
            expires_at=tx.payment_due_at,
        )
    session.flush()
    result = {
        "paused_seconds": int(delta.total_seconds()),
        "offers_extended": len(offers),
        "payments_extended": len(txs),
    }
    audit(
        session,
        AuditAction.SYSTEM_DEADLINES_EXTENDED,
        actor_type=ActorType.ADMIN if admin_id else ActorType.SYSTEM,
        actor_id=admin_id,
        details=result,
    )
    log.info("system back ONLINE: deadlines extended %s", result)
    return result
