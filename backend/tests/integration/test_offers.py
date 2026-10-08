import threading
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core import db as dbmod
from app.core.clock import utcnow
from app.core.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from app.models.enums import ItemStatus, OfferCloseReason, OfferStatus, TransactionStatus
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.user import User
from app.services.offers import service as offers
from app.services.payments import service as payments
from app.services.security.enforcement import EvasionBlocked


@pytest.fixture
def setup(db, make_user, publish_item):
    owner, bidder = make_user("Owner"), make_user("Bidder")
    item = publish_item(owner)
    return owner, bidder, item


def test_create_offer_pauses_item_and_informs_both_of_24h(db, setup, notifications):
    owner, bidder, item = setup
    res = offers.create_offer(
        db, bidder, item.public_id, offer_type="MONEY", offered_price="140000", message="Lo paso a buscar"
    )
    db.commit()
    assert res.offer.status == OfferStatus.PENDING
    assert res.offer.expires_at - res.offer.created_at == timedelta(hours=24)
    assert res.offer.owner_id == owner.id  # owner taken from the item, never from the AI
    db.refresh(item)
    assert item.status == ItemStatus.RESERVED
    assert "Tu oferta vence en 24h" in res.bidder_message
    owner_msgs = notifications(owner.id, "OFFER_RECEIVED_OWNER")
    bidder_msgs = notifications(bidder.id, "OFFER_CREATED_BIDDER")
    assert len(owner_msgs) == 1 and len(bidder_msgs) == 1
    assert "Tenés 24h para aceptar o rechazar, luego la oferta caduca" in owner_msgs[0].body
    assert "Tu oferta vence en 24h" in bidder_msgs[0].body
    assert owner_msgs[0].payload["hours"] == 24
    # Privacy: the owner does not receive the bidder's phone and vice versa.
    assert bidder.test_wa_id not in owner_msgs[0].body and owner.test_wa_id not in bidder_msgs[0].body


@pytest.mark.parametrize(
    "otype,price,offered,ok",
    [
        ("MONEY", "100", None, True),
        ("SWAP", None, "Una play 4", True),
        ("COMBINED", "50000", "Un celular", True),
        ("MONEY", None, None, False),
        ("SWAP", "100", None, False),
        ("COMBINED", None, "x", False),
    ],
)
def test_offer_types(db, setup, otype, price, offered, ok):
    _, bidder, item = setup
    if ok:
        assert (
            offers.create_offer(
                db, bidder, item.public_id, offer_type=otype, offered_price=price, offered_item=offered
            ).offer.offer_type.value
            == otype
        )
    else:
        with pytest.raises(ValidationFailed):
            offers.create_offer(db, bidder, item.public_id, offer_type=otype, offered_price=price, offered_item=offered)


def test_swap_not_accepted_and_self_offer(db, make_user, publish_item):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner, accepts_swap=False)
    with pytest.raises(InvalidState):
        offers.create_offer(db, bidder, item.public_id, offer_type="SWAP", offered_item="Una tele")
    with pytest.raises(InvalidState):
        offers.create_offer(db, owner, item.public_id, offer_type="MONEY", offered_price="1")


def test_second_offer_rejected_while_one_is_active(db, setup, make_user):
    owner, bidder, item = setup
    offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="100000")
    db.commit()
    with pytest.raises(Conflict) as e:
        offers.create_offer(db, make_user(), item.public_id, offer_type="MONEY", offered_price="200000")
    assert e.value.code == "ITEM_HAS_ACTIVE_OFFER"


def test_concurrent_offers_only_first_wins(setup, make_user):
    """Real concurrency: N threads with their own DB connections race for the same item."""
    owner, _, item = setup
    bidders = [make_user() for _ in range(6)]
    barrier = threading.Barrier(len(bidders))
    results: list[str] = []
    lock = threading.Lock()

    def attempt(user_id: int):
        session = dbmod.get_sessionmaker()()
        try:
            user = session.get(User, user_id)
            barrier.wait()
            offers.create_offer(session, user, item.public_id, offer_type="MONEY", offered_price="1000")
            session.commit()
            outcome = "OK"
        except Conflict as e:
            session.rollback()
            outcome = e.code
        except Exception as e:  # pragma: no cover - surfaced in assertion
            session.rollback()
            outcome = f"ERR:{type(e).__name__}:{e}"
        finally:
            session.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt, args=(b.id,)) for b in bidders]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results.count("OK") == 1, results
    assert results.count("ITEM_HAS_ACTIVE_OFFER") == len(bidders) - 1, results
    with dbmod.session_scope() as s:
        assert s.execute(select(Offer).where(Offer.item_id == item.id)).scalars().all().__len__() == 1


def test_db_partial_unique_index_is_the_backstop(db, setup, make_user):
    """Even bypassing the service lock, PostgreSQL refuses a second active offer for the same item."""
    owner, bidder, item = setup
    offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    db.commit()
    other = make_user()
    with pytest.raises(IntegrityError, match="uq_offers_one_active_per_item"):
        db.execute(
            text(
                "INSERT INTO offers (public_id, item_id, bidder_id, owner_id, offer_type, offered_price, "
                "status, created_at, expires_at) VALUES ('OFR-ZZZZZ', :i, :b, :o, 'MONEY', 5, 'PENDING', "
                "now(), now() + interval '1 day')"
            ),
            {"i": item.id, "b": other.id, "o": owner.id},
        )
    db.rollback()
    with pytest.raises(IntegrityError, match="fk_offers_item_owner_items"):  # owner must be the item's owner
        db.execute(
            text(
                "INSERT INTO offers (public_id, item_id, bidder_id, owner_id, offer_type, offered_price, "
                "status, created_at, expires_at) VALUES ('OFR-ZZZZY', :i, :b, :o, 'MONEY', 5, 'CANCELLED', "
                "now(), now() + interval '1 day')"
            ),
            {"i": item.id, "b": other.id, "o": bidder.id},
        )


def test_bidder_cancels(db, setup, notifications):
    owner, bidder, item = setup
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    with pytest.raises(Forbidden):
        offers.cancel_offer(db, owner, res.offer.public_id)
    offers.cancel_offer(db, bidder, res.offer.public_id)
    db.commit()
    db.refresh(item)
    assert res.offer.status == OfferStatus.CANCELLED and item.status == ItemStatus.PUBLISHED
    assert notifications(owner.id, "OFFER_CANCELLED_OWNER")


def test_owner_rejects(db, setup, notifications):
    owner, bidder, item = setup
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    with pytest.raises(Forbidden):
        offers.reject_offer(db, bidder, res.offer.public_id)
    offers.reject_offer(db, owner, res.offer.public_id)
    db.commit()
    db.refresh(item)
    assert res.offer.status == OfferStatus.REJECTED and item.status == ItemStatus.PUBLISHED
    assert notifications(bidder.id, "OFFER_REJECTED_BIDDER")
    # item is offerable again
    assert offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="2000")


def test_offer_expires_after_24h(db, setup, notifications):
    owner, bidder, item = setup
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    db.commit()
    assert offers.expire_pending_offers(db, now=utcnow() + timedelta(hours=23, minutes=59)) == 0
    assert offers.expire_pending_offers(db, now=utcnow() + timedelta(hours=24, seconds=1)) == 1
    db.commit()
    db.refresh(item)
    assert res.offer.status == OfferStatus.EXPIRED and item.status == ItemStatus.PUBLISHED
    assert notifications(bidder.id, "OFFER_EXPIRED_BIDDER") and notifications(owner.id, "OFFER_EXPIRED_OWNER")
    assert db.execute(select(Transaction)).first() is None  # no payment, no contact


def test_accepting_an_expired_offer_fails(db, setup):
    owner, bidder, item = setup
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    db.commit()
    db.execute(
        text(
            "UPDATE offers SET created_at = now() - interval '25 hours', "
            "expires_at = now() - interval '1 hour' WHERE id = :i"
        ),
        {"i": res.offer.id},
    )
    db.commit()
    with pytest.raises(InvalidState) as e:
        offers.accept_offer(db, owner, res.offer.public_id)
    assert e.value.code == "OFFER_EXPIRED"


def test_accept_creates_transaction_with_configured_fee(db, setup, notifications):
    owner, bidder, item = setup
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    with pytest.raises(Forbidden):
        offers.accept_offer(db, bidder, res.offer.public_id)
    acc = offers.accept_offer(db, owner, res.offer.public_id)
    db.commit()
    tx = acc.transaction
    assert acc.offer.status == OfferStatus.PAYMENT_PENDING and tx.status == TransactionStatus.PAYMENT_PENDING
    assert str(tx.amount) == "3000.00" and tx.currency == "ARS"
    assert tx.payment_due_at - acc.offer.accepted_at == timedelta(hours=24)
    msg = notifications(bidder.id, "OFFER_ACCEPTED_BIDDER")[0].body
    assert "ARS 3.000" in msg and "24h para pagar" in msg
    assert owner.test_wa_id not in msg  # contact NOT released on acceptance


def test_owner_can_back_out_before_payment(db, setup, notifications, gateway):
    owner, bidder, item = setup
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    acc = offers.accept_offer(db, owner, res.offer.public_id)
    link = payments.create_payment(db, bidder, acc.transaction.public_id, gateway)
    db.commit()
    offers.owner_withdraw(db, owner, res.offer.public_id)
    db.commit()
    db.refresh(item)
    db.refresh(link.payment)
    assert res.offer.status == OfferStatus.CANCELLED and res.offer.close_reason == OfferCloseReason.OWNER_WITHDREW
    assert acc.transaction.status == TransactionStatus.CANCELLED and item.status == ItemStatus.PUBLISHED
    assert link.payment.status.value == "CANCELLED"
    assert notifications(bidder.id, "OFFER_WITHDRAWN_BIDDER")


def test_owner_cannot_back_out_after_payment(db, setup, gateway):
    owner, bidder, item = setup
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    acc = offers.accept_offer(db, owner, res.offer.public_id)
    link = payments.create_payment(db, bidder, acc.transaction.public_id, gateway)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    payments.process_provider_notification(db, pp.provider_payment_id, gateway)
    db.commit()
    with pytest.raises(InvalidState) as e:
        offers.owner_withdraw(db, owner, res.offer.public_id)
    assert e.value.code == "ALREADY_PAID"


def test_payment_window_expires_after_24h(db, setup, notifications):
    owner, bidder, item = setup
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    acc = offers.accept_offer(db, owner, res.offer.public_id)
    db.commit()
    assert offers.expire_payment_windows(db, now=utcnow() + timedelta(hours=23)) == 0
    assert offers.expire_payment_windows(db, now=utcnow() + timedelta(hours=24, minutes=1)) == 1
    db.commit()
    db.refresh(item)
    assert acc.transaction.status == TransactionStatus.EXPIRED
    assert res.offer.close_reason == OfferCloseReason.PAYMENT_TIMEOUT and item.status == ItemStatus.PUBLISHED
    assert notifications(bidder.id, "PAYMENT_TIMEOUT_BIDDER") and notifications(owner.id, "PAYMENT_TIMEOUT_OWNER")


def test_bidder_active_offer_cap(db, make_user, publish_item):
    owner, bidder = make_user(), make_user()
    items = [publish_item(owner, title=f"Cosa {i}") for i in range(4)]
    for it in items[:3]:
        offers.create_offer(db, bidder, it.public_id, offer_type="MONEY", offered_price="10")
    with pytest.raises(Conflict) as e:
        offers.create_offer(db, bidder, items[3].public_id, offer_type="MONEY", offered_price="10")
    assert e.value.code == "TOO_MANY_ACTIVE_OFFERS"


def test_offer_message_with_contact_is_blocked_and_strike_persists(db, setup):
    owner, bidder, item = setup
    with pytest.raises(EvasionBlocked):
        offers.create_offer(
            db,
            bidder,
            item.public_id,
            offer_type="MONEY",
            offered_price="1000",
            message="pasame tu numero y arreglamos",
        )
    db.rollback()
    db.refresh(bidder)
    db.refresh(item)
    assert bidder.evasion_strikes == 1 and item.status == ItemStatus.PUBLISHED
    assert db.execute(select(Offer)).first() is None


def test_deleting_reserved_item_closes_offer(db, setup, notifications):
    owner, bidder, item = setup
    from app.services.publications import service as pubs

    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    db.commit()
    pubs.delete_publication(db, owner, item.id)
    db.commit()
    assert res.offer.status == OfferStatus.CANCELLED and db.get(Item, item.id).status == ItemStatus.DELETED
    assert notifications(bidder.id, "ITEM_UNAVAILABLE_BIDDER")
