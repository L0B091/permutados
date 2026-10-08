"""Release-fee payments and contact release (sections 20-22, 47).

Rules:
* A payment is confirmed ONLY when the provider API (queried with our credentials) reports ``approved``
  with the exact amount and currency of the transaction and our external_reference.
  "ya pagué", a webhook body or Dolphin's opinion never count.
* Confirmation and release are idempotent (row locks + unique indexes), safe under webhook retries/replays.
* If PAUSED/EMERGENCY, an approved payment is recorded (PAID) and the release is deferred until ONLINE.
* A payment approved after the operation was closed is flagged NEEDS_REFUND for an admin; nothing is released.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.errors import Forbidden, InvalidState, NotFound
from app.core.ids import new_security_keyword
from app.models.enums import (
    ActorType,
    ItemStatus,
    OfferStatus,
    PaymentProvider,
    PaymentStatus,
    SecurityEventType,
    Severity,
    TransactionStatus,
)
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.payment import Payment
from app.models.user import User
from app.services.audit.service import AuditAction, audit
from app.services.events.bus import EventType, get_bus
from app.services.mercadopago.gateway import PaymentGateway, ProviderPayment
from app.services.notifications import templates
from app.services.notifications.service import enqueue
from app.services.publications.state import transition_item
from app.services.security.enforcement import record_security_event
from app.services.system import config_service as cfg
from app.services.users.service import ensure_active

log = logging.getLogger(__name__)


class Outcome(StrEnum):
    CONFIRMED_AND_RELEASED = "CONFIRMED_AND_RELEASED"
    CONFIRMED_RELEASE_DEFERRED = "CONFIRMED_RELEASE_DEFERRED"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    NOT_APPROVED = "NOT_APPROVED"
    REJECTED = "REJECTED"
    NOT_FOUND_AT_PROVIDER = "NOT_FOUND_AT_PROVIDER"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    MISMATCH = "MISMATCH"
    NEEDS_REFUND = "NEEDS_REFUND"


@dataclass
class PaymentLink:
    payment: Payment
    url: str
    message: str


# ---- creation ---------------------------------------------------------------------------------
def create_payment(session: Session, bidder: User, transaction_public_id: str, gateway: PaymentGateway) -> PaymentLink:
    ensure_active(bidder)
    cfg.require_allowed(session, cfg.CriticalOp.PAYMENT_CREATE)
    tx = session.execute(
        select(Transaction)
        .where(Transaction.public_id == transaction_public_id.strip().upper())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if tx is None:
        raise NotFound("No encontré esa operación.", code="TRANSACTION_NOT_FOUND")
    if tx.bidder_id != bidder.id:
        raise Forbidden("Solo el ofertante paga la tarifa de liberación.", code="NOT_BIDDER")
    if tx.status != TransactionStatus.PAYMENT_PENDING:
        raise InvalidState(
            "Esta operación no admite pagos.", code="TRANSACTION_NOT_PAYABLE", details={"status": tx.status.value}
        )
    if tx.payment_due_at <= utcnow():
        raise InvalidState("Venció el plazo para pagar.", code="PAYMENT_WINDOW_EXPIRED")
    fee = templates.money(tx.amount, tx.currency)
    existing = (
        session.execute(
            select(Payment)
            .where(Payment.transaction_id == tx.id, Payment.status.in_([PaymentStatus.CREATED, PaymentStatus.PENDING]))
            .order_by(Payment.id.desc())
        )
        .scalars()
        .first()
    )
    if existing is not None and existing.checkout_url:
        return PaymentLink(existing, existing.checkout_url, templates.payment_link(fee=fee, url=existing.checkout_url))
    ext_ref = f"{tx.public_id}-{uuid.uuid4().hex[:12]}"
    item = session.get(Item, tx.item_id)
    checkout = gateway.create_checkout(
        external_reference=ext_ref,
        amount=tx.amount,
        currency=tx.currency,
        title=f"Permutados - liberación de contacto {item.public_id if item else ''}",
        notification_url=f"{get_settings().webhook_base_url.rstrip('/')}/webhooks/mercadopago",
        expires_at=tx.payment_due_at,
    )
    payment = Payment(
        transaction_id=tx.id,
        payer_id=bidder.id,
        provider=PaymentProvider.MERCADOPAGO,
        external_reference=ext_ref,
        provider_checkout_id=checkout.checkout_id,
        checkout_url=checkout.url,
        status=PaymentStatus.CREATED,
        amount=tx.amount,
        currency=tx.currency,
    )
    session.add(payment)
    session.flush()
    audit(
        session,
        AuditAction.PAYMENT_CREATED,
        actor_type=ActorType.USER,
        actor_id=bidder.id,
        entity_type="payment",
        entity_id=payment.id,
        details={"transaction": tx.public_id, "amount": str(tx.amount), "currency": tx.currency},
    )
    return PaymentLink(payment, checkout.url, templates.payment_link(fee=fee, url=checkout.url))


# ---- verification ----------------------------------------------------------------------------
def process_provider_notification(session: Session, provider_payment_id: str, gateway: PaymentGateway) -> Outcome:
    """Webhook entry point: re-fetch the payment from the provider and apply it."""
    pp = gateway.get_payment(str(provider_payment_id))
    if pp is None:
        audit(
            session,
            AuditAction.PAYMENT_VERIFICATION_FAILED,
            actor_type=ActorType.WEBHOOK,
            details={"provider_payment_id": str(provider_payment_id), "reason": "not_found_at_provider"},
        )
        return Outcome.NOT_FOUND_AT_PROVIDER
    return apply_provider_payment(session, pp)


def apply_provider_payment(session: Session, pp: ProviderPayment) -> Outcome:
    if not pp.external_reference:
        return Outcome.UNKNOWN_REFERENCE
    payment = session.execute(
        select(Payment).where(Payment.external_reference == pp.external_reference)
    ).scalar_one_or_none()
    if payment is None:
        record_security_event(
            session,
            user_id=None,
            event_type=SecurityEventType.PAYMENT_MISMATCH,
            severity=Severity.HIGH,
            details={"reason": "unknown_external_reference", "provider_payment_id": pp.provider_payment_id},
        )
        return Outcome.UNKNOWN_REFERENCE

    tx0 = session.get(Transaction, payment.transaction_id)
    assert tx0 is not None
    # Lock order: item -> offer -> transaction -> payment.
    item = session.execute(
        select(Item).where(Item.id == tx0.item_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()
    offer = session.execute(
        select(Offer).where(Offer.id == tx0.offer_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()
    tx = session.execute(
        select(Transaction).where(Transaction.id == tx0.id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()
    payment = session.execute(
        select(Payment).where(Payment.id == payment.id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()

    if (
        payment.provider_payment_id
        and payment.provider_payment_id != pp.provider_payment_id
        and payment.status == PaymentStatus.APPROVED
    ):
        # A second, different provider payment for the same checkout: duplicate charge.
        return _flag_refund(session, payment, tx, pp, reason="duplicate_provider_payment", clone=True)

    status = pp.internal_status
    payment.provider_status = pp.status
    payment.provider_status_detail = pp.status_detail
    payment.live_mode = pp.live_mode
    payment.provider_snapshot = pp.raw or None

    if status != PaymentStatus.APPROVED:
        if payment.status == PaymentStatus.APPROVED:
            # e.g. a later refund/chargeback notification. Never "un-release"; flag for admins.
            if status == PaymentStatus.REFUNDED:
                payment.status = PaymentStatus.REFUNDED
            session.flush()
            return Outcome.ALREADY_PROCESSED
        payment.provider_payment_id = payment.provider_payment_id or pp.provider_payment_id
        payment.status = (
            status
            if status in (PaymentStatus.REJECTED, PaymentStatus.CANCELLED, PaymentStatus.REFUNDED)
            else PaymentStatus.PENDING
        )
        session.flush()
        if status == PaymentStatus.REJECTED:
            audit(
                session,
                AuditAction.PAYMENT_REJECTED,
                actor_type=ActorType.WEBHOOK,
                entity_type="payment",
                entity_id=payment.id,
                details={"transaction": tx.public_id, "detail": pp.status_detail},
            )
            return Outcome.REJECTED
        return Outcome.NOT_APPROVED

    # ---- approved at provider: strict checks ------------------------------------------------
    if payment.status == PaymentStatus.APPROVED and payment.provider_payment_id == pp.provider_payment_id:
        if tx.status == TransactionStatus.PAID and cfg.is_allowed(session, cfg.CriticalOp.CONTACT_RELEASE):
            release_contact(session, tx)
            return Outcome.CONFIRMED_AND_RELEASED
        return Outcome.ALREADY_PROCESSED
    if pp.amount != payment.amount or pp.amount != tx.amount or pp.currency.upper() != tx.currency.upper():
        record_security_event(
            session,
            user_id=tx.bidder_id,
            event_type=SecurityEventType.PAYMENT_MISMATCH,
            severity=Severity.CRITICAL,
            details={
                "transaction": tx.public_id,
                "expected": f"{tx.amount} {tx.currency}",
                "got": f"{pp.amount} {pp.currency}",
            },
        )
        return _flag_refund(session, payment, tx, pp, reason="amount_or_currency_mismatch")
    if tx.status != TransactionStatus.PAYMENT_PENDING:
        if tx.status in (TransactionStatus.PAID, TransactionStatus.CONTACT_RELEASED):
            return _flag_refund(session, payment, tx, pp, reason="transaction_already_paid")
        record_security_event(
            session,
            user_id=tx.bidder_id,
            event_type=SecurityEventType.PAYMENT_AFTER_CLOSE,
            severity=Severity.HIGH,
            details={"transaction": tx.public_id, "tx_status": tx.status.value},
        )
        return _flag_refund(session, payment, tx, pp, reason="transaction_closed")

    now = utcnow()
    payment.status = PaymentStatus.APPROVED
    payment.provider_payment_id = pp.provider_payment_id
    payment.verified_at = now
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError:
        # Another approved payment already exists for this transaction (unique partial index).
        session.refresh(payment)
        return _flag_refund(session, payment, tx, pp, reason="transaction_already_paid")
    tx.status = TransactionStatus.PAID
    tx.paid_at = now
    offer.status = OfferStatus.PAID
    session.flush()
    audit(
        session,
        AuditAction.PAYMENT_CONFIRMED,
        actor_type=ActorType.WEBHOOK,
        entity_type="payment",
        entity_id=payment.id,
        details={
            "transaction": tx.public_id,
            "provider_payment_id": pp.provider_payment_id,
            "amount": str(pp.amount),
            "currency": pp.currency,
        },
    )
    get_bus().publish(
        session,
        EventType.PAYMENT_CONFIRMED,
        aggregate_type="transaction",
        aggregate_id=tx.public_id,
        payload={"payment_id": payment.id},
    )
    if not cfg.is_allowed(session, cfg.CriticalOp.CONTACT_RELEASE):
        log.info("payment confirmed for %s; release deferred by system mode", tx.public_id)
        return Outcome.CONFIRMED_RELEASE_DEFERRED
    release_contact(session, tx, item=item, offer=offer)
    return Outcome.CONFIRMED_AND_RELEASED


def _flag_refund(
    session: Session, payment: Payment, tx: Transaction, pp: ProviderPayment, *, reason: str, clone: bool = False
) -> Outcome:
    target = payment
    if clone:
        target = session.execute(
            select(Payment).where(
                Payment.provider == PaymentProvider.MERCADOPAGO, Payment.provider_payment_id == pp.provider_payment_id
            )
        ).scalar_one_or_none()
        if target is None:
            target = Payment(
                transaction_id=tx.id,
                payer_id=payment.payer_id,
                provider=payment.provider,
                external_reference=f"{payment.external_reference}-dup-{pp.provider_payment_id}"[:64],
                provider_payment_id=pp.provider_payment_id,
                amount=pp.amount if pp.amount > 0 else payment.amount,
                currency=pp.currency,
                status=PaymentStatus.NEEDS_REFUND,
            )
            session.add(target)
    if target.status == PaymentStatus.NEEDS_REFUND and target.provider_payment_id == pp.provider_payment_id:
        session.flush()
        return Outcome.NEEDS_REFUND if not clone else Outcome.NEEDS_REFUND
    target.status = PaymentStatus.NEEDS_REFUND
    target.provider_payment_id = target.provider_payment_id or pp.provider_payment_id
    target.provider_status = pp.status
    session.flush()
    audit(
        session,
        AuditAction.PAYMENT_NEEDS_REFUND,
        actor_type=ActorType.WEBHOOK,
        entity_type="payment",
        entity_id=target.id,
        details={"transaction": tx.public_id, "reason": reason, "provider_payment_id": pp.provider_payment_id},
    )
    return Outcome.NEEDS_REFUND if reason != "amount_or_currency_mismatch" else Outcome.MISMATCH


# ---- release -----------------------------------------------------------------------------------
def release_contact(
    session: Session, tx: Transaction, *, item: Item | None = None, offer: Offer | None = None
) -> Transaction:
    """Exchange both WhatsApp numbers + a unique keyword. Only from PAID with a verified approved payment."""
    cfg.require_allowed(session, cfg.CriticalOp.CONTACT_RELEASE)
    item = (
        item
        or session.execute(
            select(Item).where(Item.id == tx.item_id).with_for_update().execution_options(populate_existing=True)
        ).scalar_one()
    )
    offer = (
        offer
        or session.execute(
            select(Offer).where(Offer.id == tx.offer_id).with_for_update().execution_options(populate_existing=True)
        ).scalar_one()
    )
    tx = session.execute(
        select(Transaction).where(Transaction.id == tx.id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one()
    if tx.status == TransactionStatus.CONTACT_RELEASED:
        return tx  # idempotent
    if tx.status != TransactionStatus.PAID:
        raise InvalidState("No se puede liberar el contacto sin pago confirmado.", code="NOT_PAID")
    approved = session.execute(
        select(Payment).where(
            Payment.transaction_id == tx.id, Payment.status == PaymentStatus.APPROVED, Payment.verified_at.is_not(None)
        )
    ).scalar_one_or_none()
    if approved is None:
        raise InvalidState("Estado de pago ambiguo: no se libera el contacto.", code="AMBIGUOUS_PAYMENT")
    owner = session.get(User, tx.owner_id)
    bidder = session.get(User, tx.bidder_id)
    assert owner is not None and bidder is not None

    keyword = None
    for _ in range(20):
        candidate = new_security_keyword()
        try:
            with session.begin_nested():
                tx.security_keyword = candidate
                session.flush()
            keyword = candidate
            break
        except IntegrityError:
            session.refresh(tx)
            continue
    if keyword is None:  # pragma: no cover - astronomically unlikely
        raise RuntimeError("could not allocate a unique security keyword")

    now = utcnow()
    title = item.title or ""
    enqueue(
        session,
        user_id=bidder.id,
        kind="CONTACT_RELEASED_BIDDER",
        body=templates.contact_released_bidder(
            item_code=item.public_id, title=title, owner_phone=owner.phone or owner.whatsapp_id, keyword=keyword
        ),
        idempotency_key=f"tx:{tx.public_id}:released:bidder",
        template_name="contact_released",
        payload={"transaction_id": tx.public_id},
    )
    enqueue(
        session,
        user_id=owner.id,
        kind="CONTACT_RELEASED_OWNER",
        body=templates.contact_released_owner(
            item_code=item.public_id, title=title, bidder_phone=bidder.phone or bidder.whatsapp_id, keyword=keyword
        ),
        idempotency_key=f"tx:{tx.public_id}:released:owner",
        template_name="contact_released",
        payload={"transaction_id": tx.public_id},
    )
    tx.status = TransactionStatus.CONTACT_RELEASED
    tx.released_at = now
    tx.closed_at = now
    offer.status = OfferStatus.CONTACT_RELEASED
    offer.closed_at = now
    session.flush()
    audit(
        session,
        AuditAction.CONTACT_RELEASED,
        actor_type=ActorType.SYSTEM,
        entity_type="transaction",
        entity_id=tx.public_id,
        details={"item": item.public_id, "offer": offer.public_id},
    )
    if item.status == ItemStatus.RESERVED:
        transition_item(
            session, item, ItemStatus.COMPLETED, actor_type=ActorType.SYSTEM, reason=f"CONTACT_RELEASED:{tx.public_id}"
        )
    get_bus().publish(
        session,
        EventType.CONTACT_RELEASED,
        aggregate_type="transaction",
        aggregate_id=tx.public_id,
        payload={"item": item.public_id},
    )
    return tx


# ---- reconciliation / user queries --------------------------------------------------------------
def sync_transaction_payments(session: Session, tx: Transaction, gateway: PaymentGateway) -> list[Outcome]:
    """Ask the provider about every checkout of this transaction (used by "ya pagué", reconciliation and
    before an owner withdraws)."""
    refs = session.execute(select(Payment.external_reference).where(Payment.transaction_id == tx.id)).scalars().all()
    outcomes = []
    for ref in refs:
        for pp in gateway.search_payments(ref):
            outcomes.append(apply_provider_payment(session, pp))
    return outcomes


def release_deferred(session: Session) -> int:
    """Release PAID transactions whose release was deferred by PAUSED/EMERGENCY mode."""
    if not cfg.is_allowed(session, cfg.CriticalOp.CONTACT_RELEASE):
        return 0
    txs = session.execute(select(Transaction).where(Transaction.status == TransactionStatus.PAID)).scalars().all()
    for tx in txs:
        release_contact(session, tx)
    return len(txs)


def reconcile_pending_payments(session: Session, gateway: PaymentGateway) -> int:
    """Recover approvals that happened while we were down (webhook lost)."""
    txs = (
        session.execute(select(Transaction).where(Transaction.status == TransactionStatus.PAYMENT_PENDING))
        .scalars()
        .all()
    )
    n = 0
    for tx in txs:
        if any(
            o in (Outcome.CONFIRMED_AND_RELEASED, Outcome.CONFIRMED_RELEASE_DEFERRED)
            for o in sync_transaction_payments(session, tx, gateway)
        ):
            n += 1
    return n


def get_payment_status(
    session: Session, user: User, transaction_public_id: str, gateway: PaymentGateway | None = None
) -> dict:
    """What the user (or Dolphin on their behalf) may know. A claim like "ya pagué" only triggers a
    verified provider lookup; it never changes state by itself."""
    tx = session.execute(
        select(Transaction).where(Transaction.public_id == transaction_public_id.strip().upper())
    ).scalar_one_or_none()
    if tx is None or user.id not in (tx.bidder_id, tx.owner_id):
        raise NotFound("No encontré esa operación.", code="TRANSACTION_NOT_FOUND")
    if gateway is not None and tx.status == TransactionStatus.PAYMENT_PENDING:
        sync_transaction_payments(session, tx, gateway)
        session.refresh(tx)
    return {
        "transaction_id": tx.public_id,
        "status": tx.status.value,
        "amount": str(tx.amount),
        "currency": tx.currency,
        "payment_due_at": tx.payment_due_at.isoformat(),
        "paid": tx.status in (TransactionStatus.PAID, TransactionStatus.CONTACT_RELEASED),
    }


def transaction_view(session: Session, user: User, transaction_public_id: str) -> dict:
    tx = session.execute(
        select(Transaction).where(Transaction.public_id == transaction_public_id.strip().upper())
    ).scalar_one_or_none()
    if tx is None or user.id not in (tx.bidder_id, tx.owner_id):
        raise NotFound("No encontré esa operación.", code="TRANSACTION_NOT_FOUND")
    item = session.get(Item, tx.item_id)
    return {
        "transaction_id": tx.public_id,
        "item_id": item.public_id if item else None,
        "role": "bidder" if user.id == tx.bidder_id else "owner",
        "status": tx.status.value,
        "amount": str(tx.amount),
        "currency": tx.currency,
        "payment_due_at": tx.payment_due_at.isoformat(),
    }
