"""Offers (sections 17-20) with the user decisions applied:

* exactly one active offer per item (row lock + PostgreSQL partial unique index);
* while an offer is PENDING the item is paused (RESERVED: hidden from search, no other offers);
* the owner has ``offer_expiration_hours`` (24h) to accept/reject, both parties are told so up front;
* after acceptance the bidder has ``payment_window_hours`` (24h) to pay the release fee;
* the owner may back out after accepting, but only while the bidder has not paid.

Lock order everywhere: item -> offer -> transaction -> payment (prevents deadlocks).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from app.core.ids import new_offer_code, new_transaction_code
from app.core.text import clean_user_text
from app.models.enums import (
    ACTIVE_OFFER_STATUSES,
    ActorType,
    ItemStatus,
    OfferCloseReason,
    OfferStatus,
    OfferType,
    PaymentStatus,
    TransactionStatus,
    UserStatus,
)
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.payment import Payment
from app.models.user import User
from app.services.audit.service import AuditAction, audit
from app.services.events.bus import EventType, get_bus
from app.services.notifications import templates
from app.services.notifications.service import enqueue
from app.services.publications.service import parse_price
from app.services.publications.state import transition_item
from app.services.security.enforcement import enforce_or_raise
from app.services.system import config_service as cfg
from app.services.users.service import ensure_active

UNPAID_ACTIVE = (OfferStatus.PENDING, OfferStatus.ACCEPTED, OfferStatus.PAYMENT_PENDING)


@dataclass
class OfferResult:
    offer: Offer
    bidder_message: str
    owner_message: str | None = None
    transaction: Transaction | None = None


# ---- lookup helpers ------------------------------------------------------------------------
def _lock_item(session: Session, item_id: int) -> Item:
    return session.execute(
        select(Item).where(Item.id == item_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()


def _lock_offer(session: Session, offer_id: int) -> Offer:
    return session.execute(
        select(Offer).where(Offer.id == offer_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()


def _offer_by_code(session: Session, public_id: str) -> Offer:
    offer = session.execute(
        select(Offer).where(Offer.public_id == (public_id or "").strip().upper())
    ).scalar_one_or_none()
    if offer is None:
        raise NotFound("No encontré esa oferta.", code="OFFER_NOT_FOUND")
    return offer


def _lock_chain(session: Session, public_id: str) -> tuple[Item, Offer]:
    """Lock item then offer, re-reading the offer after the lock."""
    offer = _offer_by_code(session, public_id)
    item = _lock_item(session, offer.item_id)
    offer = _lock_offer(session, offer.id)
    return item, offer


def get_transaction_for_offer(session: Session, offer_id: int, *, for_update: bool = False) -> Transaction | None:
    stmt = select(Transaction).where(Transaction.offer_id == offer_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return session.execute(stmt).scalar_one_or_none()


def get_offer_for_user(session: Session, user: User, public_id: str) -> Offer:
    offer = _offer_by_code(session, public_id)
    if user.id not in (offer.bidder_id, offer.owner_id):
        raise Forbidden("Esa oferta no te pertenece.", code="NOT_PARTICIPANT")
    return offer


def offer_view(session: Session, offer: Offer, viewer: User) -> dict[str, Any]:
    item = session.get(Item, offer.item_id)
    tx = get_transaction_for_offer(session, offer.id)
    return {
        "offer_id": offer.public_id,
        "item_id": item.public_id if item else None,
        "item_title": item.title if item else None,
        "role": "bidder" if viewer.id == offer.bidder_id else "owner",
        "offer_type": offer.offer_type.value,
        "offered_price": str(offer.offered_price) if offer.offered_price is not None else None,
        "offered_item": offer.offered_item,
        "message": offer.message,
        "status": offer.status.value,
        "expires_at": offer.expires_at.isoformat(),
        "transaction_id": tx.public_id if tx else None,
    }


# ---- create ----------------------------------------------------------------------------------
def create_offer(
    session: Session,
    bidder: User,
    item_public_id: str,
    *,
    offer_type: str | OfferType,
    offered_price: Any = None,
    offered_item: str | None = None,
    message: str | None = None,
) -> OfferResult:
    ensure_active(bidder)
    cfg.require_allowed(session, cfg.CriticalOp.OFFER)
    try:
        otype = OfferType(str(offer_type).upper())
    except ValueError as e:
        raise ValidationFailed("tipo de oferta inválido", code="INVALID_OFFER_TYPE") from e
    price = parse_price(offered_price) if offered_price not in (None, "") else None
    offered_item = clean_user_text(offered_item, 300)
    message = clean_user_text(message, 500)
    if otype == OfferType.MONEY and (price is None or offered_item):
        raise ValidationFailed("Una oferta económica necesita solo un monto.", code="INVALID_OFFER_FIELDS")
    if otype == OfferType.SWAP and (price is not None or not offered_item):
        raise ValidationFailed("Una permuta necesita describir qué ofrecés a cambio.", code="INVALID_OFFER_FIELDS")
    if otype == OfferType.COMBINED and (price is None or not offered_item):
        raise ValidationFailed("Una oferta combinada necesita monto y artículo.", code="INVALID_OFFER_FIELDS")

    # Anti-evasion BEFORE taking locks or writing anything (it commits the strike if triggered).
    enforce_or_raise(session, bidder.id, offered_item, message, source="offer")

    max_active = int(cfg.get_value(session, "max_active_offers_per_bidder"))
    active_count = session.execute(
        select(func.count())
        .select_from(Offer)
        .where(Offer.bidder_id == bidder.id, Offer.status.in_(ACTIVE_OFFER_STATUSES))
    ).scalar_one()
    if active_count >= max_active:
        raise Conflict(
            f"Ya tenés {active_count} ofertas activas. Esperá una respuesta o cancelá alguna.",
            code="TOO_MANY_ACTIVE_OFFERS",
        )

    code = (item_public_id or "").strip().upper()
    item = session.execute(
        select(Item).where(Item.public_id == code).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if item is None:
        raise NotFound("No encontré ese artículo.", code="ITEM_NOT_FOUND")
    if item.owner_id == bidder.id:
        raise InvalidState("No podés ofertar por tu propio artículo.", code="OWN_ITEM")
    now = utcnow()
    if item.status == ItemStatus.RESERVED:
        raise Conflict(
            "Este artículo ya tiene una oferta activa. Probá más tarde o elegí otro.", code="ITEM_HAS_ACTIVE_OFFER"
        )
    if item.status != ItemStatus.PUBLISHED or (item.expires_at and item.expires_at <= now):
        raise InvalidState("Ese artículo ya no está disponible.", code="ITEM_NOT_AVAILABLE")
    if otype in (OfferType.SWAP, OfferType.COMBINED) and not item.accepts_swap:
        raise InvalidState("El propietario no acepta permutas para este artículo.", code="SWAP_NOT_ACCEPTED")
    owner = session.get(User, item.owner_id)
    if owner is None or owner.status != UserStatus.ACTIVE:
        raise InvalidState("Ese artículo ya no está disponible.", code="ITEM_NOT_AVAILABLE")

    hours = cfg.offer_hours(session)
    offer = Offer(
        public_id=new_offer_code(),
        item_id=item.id,
        bidder_id=bidder.id,
        owner_id=item.owner_id,
        offer_type=otype,
        offered_price=price,
        offered_item=offered_item,
        message=message,
        status=OfferStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(hours=hours),
    )
    try:
        with session.begin_nested():
            session.add(offer)
            session.flush()
    except IntegrityError as e:
        # Backstop: the partial unique index rejected a concurrent second active offer.
        if "uq_offers_one_active_per_item" in str(e.orig):
            raise Conflict("Este artículo ya tiene una oferta activa.", code="ITEM_HAS_ACTIVE_OFFER") from e
        raise
    item.offer_count += 1
    transition_item(
        session,
        item,
        ItemStatus.RESERVED,
        actor_type=ActorType.USER,
        actor_id=bidder.id,
        reason=f"OFFER:{offer.public_id}",
    )
    audit(
        session,
        AuditAction.OFFER_CREATED,
        actor_type=ActorType.USER,
        actor_id=bidder.id,
        entity_type="offer",
        entity_id=offer.public_id,
        details={"item": item.public_id, "type": otype.value, "price": str(price) if price else None},
    )
    get_bus().publish(
        session,
        EventType.OFFER_CREATED,
        aggregate_type="offer",
        aggregate_id=offer.public_id,
        payload={"item": item.public_id},
    )

    summary = templates.offer_summary(otype.value, price, offered_item, item.currency)
    bidder_msg = templates.offer_created_bidder(
        item_code=item.public_id, title=item.title or "", summary=summary, hours=hours, expires_at=offer.expires_at
    )
    owner_msg = templates.offer_received_owner(
        item_code=item.public_id,
        title=item.title or "",
        summary=summary,
        message=message,
        hours=hours,
        expires_at=offer.expires_at,
    )
    enqueue(
        session,
        user_id=bidder.id,
        kind="OFFER_CREATED_BIDDER",
        body=bidder_msg,
        idempotency_key=f"offer:{offer.public_id}:created:bidder",
        payload={"offer_id": offer.public_id, "expires_at": offer.expires_at.isoformat(), "hours": hours},
        expires_at=offer.expires_at,
    )
    enqueue(
        session,
        user_id=item.owner_id,
        kind="OFFER_RECEIVED_OWNER",
        body=owner_msg,
        idempotency_key=f"offer:{offer.public_id}:created:owner",
        payload={"offer_id": offer.public_id, "expires_at": offer.expires_at.isoformat(), "hours": hours},
        expires_at=offer.expires_at,
    )
    return OfferResult(offer, bidder_msg, owner_msg)


# ---- closing helpers -----------------------------------------------------------------------
def _close_offer(
    session: Session,
    item: Item,
    offer: Offer,
    status: OfferStatus,
    reason: OfferCloseReason | None,
    *,
    actor_type: ActorType,
    actor_id: int | None,
    action: AuditAction,
) -> None:
    now = utcnow()
    offer.status = status
    offer.close_reason = reason
    offer.closed_at = now
    if status in (OfferStatus.REJECTED,):
        offer.responded_at = now
    tx = get_transaction_for_offer(session, offer.id, for_update=True)
    if tx is not None and tx.status == TransactionStatus.PAYMENT_PENDING:
        tx.status = (
            TransactionStatus.EXPIRED if reason == OfferCloseReason.PAYMENT_TIMEOUT else TransactionStatus.CANCELLED
        )
        tx.closed_at = now
        tx.close_reason = (reason or status).value
        session.execute(
            update(Payment)
            .where(Payment.transaction_id == tx.id, Payment.status.in_([PaymentStatus.CREATED, PaymentStatus.PENDING]))
            .values(status=PaymentStatus.CANCELLED)
        )
        if reason == OfferCloseReason.PAYMENT_TIMEOUT:
            audit(
                session,
                AuditAction.TRANSACTION_EXPIRED,
                actor_type=actor_type,
                actor_id=actor_id,
                entity_type="transaction",
                entity_id=tx.public_id,
            )
    session.flush()
    if item.status == ItemStatus.RESERVED:
        transition_item(
            session,
            item,
            ItemStatus.PUBLISHED,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=f"OFFER_{status.value}",
        )
    audit(
        session,
        action,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="offer",
        entity_id=offer.public_id,
        details={"status": status.value, "reason": reason.value if reason else None, "item": item.public_id},
    )
    get_bus().publish(
        session,
        EventType.OFFER_CLOSED,
        aggregate_type="offer",
        aggregate_id=offer.public_id,
        payload={"status": status.value, "reason": reason.value if reason else None},
    )


def _assert_not_paid(session: Session, offer: Offer) -> None:
    if offer.status in (OfferStatus.PAID, OfferStatus.CONTACT_RELEASED):
        raise InvalidState("El pago ya fue acreditado: la operación no se puede retirar.", code="ALREADY_PAID")
    tx = get_transaction_for_offer(session, offer.id)
    if tx is not None:
        approved = session.execute(
            select(Payment.id).where(Payment.transaction_id == tx.id, Payment.status == PaymentStatus.APPROVED)
        ).first()
        if approved or tx.status != TransactionStatus.PAYMENT_PENDING:
            raise InvalidState("El pago ya fue acreditado: la operación no se puede retirar.", code="ALREADY_PAID")


# ---- bidder / owner actions ------------------------------------------------------------------
def cancel_offer(session: Session, bidder: User, offer_public_id: str) -> OfferResult:
    """The bidder can cancel before acceptance, and also after acceptance as long as they have not paid."""
    item, offer = _lock_chain(session, offer_public_id)
    if offer.bidder_id != bidder.id:
        raise Forbidden("Solo quien hizo la oferta puede cancelarla.", code="NOT_BIDDER")
    if offer.status not in UNPAID_ACTIVE:
        if offer.status in (OfferStatus.PAID, OfferStatus.CONTACT_RELEASED):
            raise InvalidState("La oferta ya fue pagada.", code="ALREADY_PAID")
        raise InvalidState("Esa oferta ya está cerrada.", code="OFFER_CLOSED")
    _assert_not_paid(session, offer)
    _close_offer(
        session,
        item,
        offer,
        OfferStatus.CANCELLED,
        OfferCloseReason.BIDDER_CANCELLED,
        actor_type=ActorType.USER,
        actor_id=bidder.id,
        action=AuditAction.OFFER_CANCELLED,
    )
    enqueue(
        session,
        user_id=offer.owner_id,
        kind="OFFER_CANCELLED_OWNER",
        body=templates.offer_cancelled_owner(item_code=item.public_id, title=item.title or ""),
        idempotency_key=f"offer:{offer.public_id}:cancelled:owner",
    )
    return OfferResult(offer, "Cancelaste tu oferta. No se cobró nada.")


def accept_offer(session: Session, owner: User, offer_public_id: str) -> OfferResult:
    ensure_active(owner)
    cfg.require_allowed(session, cfg.CriticalOp.ACCEPT)
    item, offer = _lock_chain(session, offer_public_id)
    if offer.owner_id != owner.id or item.owner_id != owner.id:
        raise Forbidden("Solo el propietario puede aceptar la oferta.", code="NOT_OWNER")
    now = utcnow()
    if offer.status == OfferStatus.PENDING and offer.expires_at <= now:
        _expire_offer_locked(session, item, offer)
        raise InvalidState("La oferta ya caducó.", code="OFFER_EXPIRED")
    if offer.status != OfferStatus.PENDING:
        raise InvalidState("Esa oferta ya no está pendiente.", code="OFFER_NOT_PENDING")
    bidder = session.get(User, offer.bidder_id)
    if bidder is None or bidder.status != UserStatus.ACTIVE:
        raise InvalidState("El ofertante ya no está disponible.", code="BIDDER_UNAVAILABLE")

    offer.status = OfferStatus.ACCEPTED
    offer.accepted_at = now
    offer.responded_at = now
    session.flush()
    amount, currency = cfg.release_price(session)
    hours = cfg.payment_hours(session)
    tx = Transaction(
        public_id=new_transaction_code(),
        offer_id=offer.id,
        item_id=item.id,
        owner_id=offer.owner_id,
        bidder_id=offer.bidder_id,
        status=TransactionStatus.PAYMENT_PENDING,
        amount=amount,
        currency=currency,
        payment_due_at=now + timedelta(hours=hours),
    )
    session.add(tx)
    offer.status = OfferStatus.PAYMENT_PENDING
    session.flush()
    audit(
        session,
        AuditAction.OFFER_ACCEPTED,
        actor_type=ActorType.USER,
        actor_id=owner.id,
        entity_type="offer",
        entity_id=offer.public_id,
        details={"transaction": tx.public_id, "fee": str(amount), "currency": currency},
    )
    get_bus().publish(
        session,
        EventType.OFFER_ACCEPTED,
        aggregate_type="offer",
        aggregate_id=offer.public_id,
        payload={"transaction": tx.public_id},
    )
    fee = templates.money(amount, currency)
    bidder_msg = templates.offer_accepted_bidder(
        item_code=item.public_id, title=item.title or "", fee=fee, hours=hours, due_at=tx.payment_due_at
    )
    owner_msg = templates.offer_accepted_owner(item_code=item.public_id, title=item.title or "", hours=hours)
    enqueue(
        session,
        user_id=offer.bidder_id,
        kind="OFFER_ACCEPTED_BIDDER",
        body=bidder_msg,
        idempotency_key=f"offer:{offer.public_id}:accepted:bidder",
        payload={"transaction_id": tx.public_id},
        expires_at=tx.payment_due_at,
    )
    enqueue(
        session,
        user_id=owner.id,
        kind="OFFER_ACCEPTED_OWNER",
        body=owner_msg,
        idempotency_key=f"offer:{offer.public_id}:accepted:owner",
        expires_at=tx.payment_due_at,
    )
    return OfferResult(offer, bidder_msg, owner_msg, tx)


def reject_offer(session: Session, owner: User, offer_public_id: str) -> OfferResult:
    item, offer = _lock_chain(session, offer_public_id)
    if offer.owner_id != owner.id:
        raise Forbidden("Solo el propietario puede rechazar la oferta.", code="NOT_OWNER")
    if offer.status != OfferStatus.PENDING:
        raise InvalidState("Esa oferta ya no está pendiente.", code="OFFER_NOT_PENDING")
    _close_offer(
        session,
        item,
        offer,
        OfferStatus.REJECTED,
        None,
        actor_type=ActorType.USER,
        actor_id=owner.id,
        action=AuditAction.OFFER_REJECTED,
    )
    enqueue(
        session,
        user_id=offer.bidder_id,
        kind="OFFER_REJECTED_BIDDER",
        body=templates.offer_rejected_bidder(item_code=item.public_id, title=item.title or ""),
        idempotency_key=f"offer:{offer.public_id}:rejected:bidder",
    )
    return OfferResult(offer, "", "Rechazaste la oferta. Tu publicación vuelve a estar activa.")


def owner_withdraw(session: Session, owner: User, offer_public_id: str) -> OfferResult:
    """Owner backs out after accepting. Allowed only while the bidder has NOT paid (user decision).

    Callers should first sync the provider (payments.service.sync_transaction_payments) so a payment that was
    just approved at the provider is detected; if it is, this raises ALREADY_PAID.
    """
    item, offer = _lock_chain(session, offer_public_id)
    if offer.owner_id != owner.id:
        raise Forbidden("Solo el propietario puede retirarse de la operación.", code="NOT_OWNER")
    if offer.status == OfferStatus.PENDING:
        raise InvalidState("La oferta todavía no fue aceptada: podés rechazarla.", code="OFFER_NOT_ACCEPTED")
    if offer.status not in (OfferStatus.ACCEPTED, OfferStatus.PAYMENT_PENDING):
        if offer.status in (OfferStatus.PAID, OfferStatus.CONTACT_RELEASED):
            raise InvalidState("El pago ya fue acreditado: la operación no se puede retirar.", code="ALREADY_PAID")
        raise InvalidState("Esa oferta ya está cerrada.", code="OFFER_CLOSED")
    _assert_not_paid(session, offer)
    _close_offer(
        session,
        item,
        offer,
        OfferStatus.CANCELLED,
        OfferCloseReason.OWNER_WITHDREW,
        actor_type=ActorType.USER,
        actor_id=owner.id,
        action=AuditAction.OFFER_WITHDRAWN_BY_OWNER,
    )
    enqueue(
        session,
        user_id=offer.bidder_id,
        kind="OFFER_WITHDRAWN_BIDDER",
        body=templates.offer_withdrawn_bidder(item_code=item.public_id, title=item.title or ""),
        idempotency_key=f"offer:{offer.public_id}:withdrawn:bidder",
    )
    return OfferResult(offer, "", "Te retiraste de la operación. Tu publicación vuelve a estar activa.")


def close_active_offer_for_item(
    session: Session, item: Item, *, reason: OfferCloseReason, actor_type: ActorType, actor_id: int | None
) -> Offer | None:
    """Used when an item is deleted/removed while reserved. Refuses if the fee was already paid."""
    offer = session.execute(
        select(Offer)
        .where(Offer.item_id == item.id, Offer.status.in_(ACTIVE_OFFER_STATUSES))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if offer is None:
        return None
    _assert_not_paid(session, offer)
    _close_offer(
        session,
        item,
        offer,
        OfferStatus.CANCELLED,
        reason,
        actor_type=actor_type,
        actor_id=actor_id,
        action=AuditAction.OFFER_CANCELLED,
    )
    enqueue(
        session,
        user_id=offer.bidder_id,
        kind="ITEM_UNAVAILABLE_BIDDER",
        body=templates.item_unavailable_bidder(item_code=item.public_id),
        idempotency_key=f"offer:{offer.public_id}:item_unavailable:bidder",
    )
    return offer


def close_active_offers_for_user(session: Session, user_id: int, *, reason: OfferCloseReason) -> int:
    """Close unpaid active offers where the user is bidder or owner (e.g. account suspended)."""
    ids = (
        session.execute(
            select(Offer.public_id).where(
                or_(Offer.bidder_id == user_id, Offer.owner_id == user_id), Offer.status.in_(UNPAID_ACTIVE)
            )
        )
        .scalars()
        .all()
    )
    closed = 0
    for code in ids:
        item, offer = _lock_chain(session, code)
        if offer.status not in UNPAID_ACTIVE:
            continue
        try:
            _assert_not_paid(session, offer)
        except InvalidState:
            continue
        _close_offer(
            session,
            item,
            offer,
            OfferStatus.CANCELLED,
            reason,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action=AuditAction.OFFER_CANCELLED,
        )
        other = offer.owner_id if offer.bidder_id == user_id else offer.bidder_id
        enqueue(
            session,
            user_id=other,
            kind="OFFER_CLOSED_COUNTERPART_BLOCKED",
            body=f"La operación por {item.title} ({item.public_id}) se cerró. No se cobró nada.",
            idempotency_key=f"offer:{offer.public_id}:closed_blocked:{other}",
        )
        closed += 1
    return closed


# ---- expiry (called by workers) --------------------------------------------------------------
def _expire_offer_locked(session: Session, item: Item, offer: Offer) -> None:
    hours = cfg.offer_hours(session)
    _close_offer(
        session,
        item,
        offer,
        OfferStatus.EXPIRED,
        None,
        actor_type=ActorType.WORKER,
        actor_id=None,
        action=AuditAction.OFFER_EXPIRED,
    )
    enqueue(
        session,
        user_id=offer.bidder_id,
        kind="OFFER_EXPIRED_BIDDER",
        body=templates.offer_expired_bidder(item_code=item.public_id, title=item.title or "", hours=hours),
        idempotency_key=f"offer:{offer.public_id}:expired:bidder",
    )
    enqueue(
        session,
        user_id=offer.owner_id,
        kind="OFFER_EXPIRED_OWNER",
        body=templates.offer_expired_owner(item_code=item.public_id, title=item.title or "", hours=hours),
        idempotency_key=f"offer:{offer.public_id}:expired:owner",
    )


def expire_pending_offers(session: Session, now=None) -> int:
    now = now or utcnow()
    codes = (
        session.execute(select(Offer.public_id).where(Offer.status == OfferStatus.PENDING, Offer.expires_at <= now))
        .scalars()
        .all()
    )
    n = 0
    for code in codes:
        item, offer = _lock_chain(session, code)
        if offer.status == OfferStatus.PENDING and offer.expires_at <= now:
            _expire_offer_locked(session, item, offer)
            n += 1
    return n


def expire_payment_windows(session: Session, now=None) -> int:
    """Accepted offers whose bidder did not pay in time: cancel, item back to PUBLISHED."""
    now = now or utcnow()
    rows = (
        session.execute(
            select(Offer.public_id)
            .join(Transaction, Transaction.offer_id == Offer.id)
            .where(
                Transaction.status == TransactionStatus.PAYMENT_PENDING,
                Transaction.payment_due_at <= now,
                Offer.status.in_([OfferStatus.ACCEPTED, OfferStatus.PAYMENT_PENDING]),
            )
        )
        .scalars()
        .all()
    )
    n = 0
    for code in rows:
        item, offer = _lock_chain(session, code)
        tx = get_transaction_for_offer(session, offer.id, for_update=True)
        if tx is None or tx.status != TransactionStatus.PAYMENT_PENDING or tx.payment_due_at > now:
            continue
        approved = session.execute(
            select(Payment.id).where(Payment.transaction_id == tx.id, Payment.status == PaymentStatus.APPROVED)
        ).first()
        if approved:
            continue  # paid: never cancel; release is handled by the payment flow / reconciliation
        _close_offer(
            session,
            item,
            offer,
            OfferStatus.CANCELLED,
            OfferCloseReason.PAYMENT_TIMEOUT,
            actor_type=ActorType.WORKER,
            actor_id=None,
            action=AuditAction.OFFER_CANCELLED,
        )
        enqueue(
            session,
            user_id=offer.bidder_id,
            kind="PAYMENT_TIMEOUT_BIDDER",
            body=templates.payment_timeout_bidder(item_code=item.public_id, title=item.title or ""),
            idempotency_key=f"offer:{offer.public_id}:payment_timeout:bidder",
        )
        enqueue(
            session,
            user_id=offer.owner_id,
            kind="PAYMENT_TIMEOUT_OWNER",
            body=templates.payment_timeout_owner(item_code=item.public_id, title=item.title or ""),
            idempotency_key=f"offer:{offer.public_id}:payment_timeout:owner",
        )
        n += 1
    return n


def active_offer_for_item(session: Session, item_id: int) -> Offer | None:
    return session.execute(
        select(Offer).where(Offer.item_id == item_id, Offer.status.in_(ACTIVE_OFFER_STATUSES))
    ).scalar_one_or_none()
