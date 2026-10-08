"""PHASE 4 end-to-end branch cases (spec section 50), all through the real pipeline: signed WhatsApp webhooks ->
durable inbound queue -> conversation engine (scripted mock Dolphin proposing tools, backend deciding) -> outbox ->
WhatsApp client (recording mock), signed Mercado Pago webhooks (fake gateway), the real worker jobs (run_job: mode
policy, advisory locks, bookkeeping) and the real admin HTTP API. Real PostgreSQL + Redis.

Covered here: rejection, 24h without answer, accepted but not paid, fake payments, duplicate webhooks, second
offer, cancellations (bidder / owner backs out before paying / not after), first evasion warning, second evasion ->
admin review, WhatsApp send error, Mercado Pago errors, Dolphin down then back, server restart recovery and
channel reconciliation. The happy path is in test_e2e_flow.py / test_whatsapp_e2e.py.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from app.core import db as dbmod
from app.core.clock import utcnow
from app.models.channel import ChannelPost
from app.models.conversation import Notification
from app.models.enums import (
    AdminRole,
    BanStatus,
    ChannelPostStatus,
    ItemStatus,
    JobRunStatus,
    OfferStatus,
    PaymentStatus,
    TransactionStatus,
    UserStatus,
)
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.payment import Payment
from app.models.security import Ban, Warning
from app.models.user import User
from app.services.admin.auth import create_admin
from app.services.channel import service as channel
from app.services.conversation.inbound_queue import process_inbound_queue
from app.services.dolphin.mock import say, tool
from app.services.dolphin.orchestrator import DOLPHIN_DOWN_REPLY
from app.services.mercadopago.signature import compute_signature
from app.services.notifications import templates
from app.services.notifications.outbox import dispatch_outbox
from app.services.whatsapp.client import WhatsAppError
from app.workers.jobs import maintenance_jobs
from app.workers.runtime import run_job
from tests.e2e.test_whatsapp_e2e import Bot
from tests.helpers import wa_payloads as P


# ---- harness ------------------------------------------------------------------------------------------------
class Journey:
    """One owner with a published item, one (or more) bidders, all talking through signed webhooks."""

    def __init__(self, db, bot, client, gateway, make_user, publish_item):
        self.db, self.bot, self.client, self.gateway = db, bot, client, gateway
        self.make_user = make_user
        self.owner = make_user("Olga")
        self.bidder = make_user("Bruno")
        self.item = publish_item(self.owner, title="Bicicleta Trek Marlin 7", price="250000")
        self.code, self.item_id = self.item.public_id, self.item.id  # plain values survive a session restart
        self._req = 0
        # Both parties have written to the bot recently (open 24h WhatsApp window -> free-form messages).
        self.bot.turn(P.text(self.owner.test_wa_id, "hola")[0], say("¡Hola!"))
        self.bot.turn(P.text(self.bidder.test_wa_id, "hola")[0], say("¡Hola!"))

    # conversation steps
    def offer(self, user=None, price="230000"):
        user = user or self.bidder
        self.bot.turn(
            P.text(user.test_wa_id, f"me interesa {self.code}")[0],
            tool("select_publication", reference=self.code),
            say("Bicicleta Trek Marlin 7."),
        )
        return self.bot.turn(
            P.text(user.test_wa_id, f"ofrezco {price}")[0],
            tool("create_offer", offer_type="MONEY", offered_price=price),
            say("Listo."),
        )

    def owner_says(self, text, *script):
        return self.bot.turn(P.text(self.owner.test_wa_id, text)[0], *script)

    def bidder_says(self, text, *script, user=None):
        return self.bot.turn(P.text((user or self.bidder).test_wa_id, text)[0], *script)

    def accept(self):
        return self.owner_says("ACEPTAR", tool("accept_offer"), say("Aceptaste."))

    def pay_link(self):
        self.bidder_says("PAGAR", tool("create_payment"), say("Ahí va el link."))
        self.db.expire_all()
        return self.db.execute(select(Payment).order_by(Payment.id.desc())).scalars().first()

    def mp_webhook(self, provider_payment_id, request_id=None, *, signature_secret="test-mp-webhook-secret"):
        self._req += 1
        request_id = request_id or f"req-{self._req}"
        ts = str(int(time.time()))
        sig = compute_signature(signature_secret, provider_payment_id, request_id, ts)
        return self.client.post(
            f"/webhooks/mercadopago?data.id={provider_payment_id}&type=payment",
            json={"type": "payment", "data": {"id": provider_payment_id}},
            headers={"x-signature": f"ts={ts},v1={sig}", "x-request-id": request_id},
        )

    # state
    def fresh(self, model, pk):
        self.db.expire_all()
        return self.db.get(model, pk)

    def offers_of(self, user):
        self.db.expire_all()
        return list(self.db.execute(select(Offer).where(Offer.bidder_id == user.id).order_by(Offer.id)).scalars())

    def tx(self):
        self.db.expire_all()
        return self.db.execute(select(Transaction).order_by(Transaction.id.desc())).scalars().first()

    def item_status(self):
        return self.fresh(Item, self.item_id).status

    def inbox(self, user):
        return self.bot.inbox(user.test_wa_id)

    def released_messages(self):
        self.db.expire_all()
        return self.db.execute(
            select(func.count()).select_from(Notification).where(Notification.kind.like("CONTACT_RELEASED%"))
        ).scalar_one()

    def contact_leaked(self):
        """Did either party receive the other's number?"""
        return any(self.owner.test_wa_id in t for t in self.inbox(self.bidder)) or any(
            self.bidder.test_wa_id in t for t in self.inbox(self.owner)
        )


@pytest.fixture
def j(db, wa_post, wa_client, gateway, client, make_user, publish_item):
    return Journey(db, Bot(wa_post, wa_client, gateway), client, gateway, make_user, publish_item)


def _job(name, gateway=None):
    return {x.name: x for x in maintenance_jobs(gateway)}[name]


def _admin_token(db, client, role=AdminRole.ADMIN):
    create_admin(db, f"adm-{role.value.lower()}", "admin-password-123456", role)
    db.commit()
    r = client.post(
        "/admin/auth/login", json={"username": f"adm-{role.value.lower()}", "password": "admin-password-123456"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---- offer lifecycle branches -------------------------------------------------------------------------------
def test_owner_rejects_the_offer(j):
    j.offer()
    assert j.item_status() == ItemStatus.RESERVED  # paused while the owner decides
    j.owner_says("RECHAZAR", tool("reject_offer"), say("Rechazaste la oferta."))
    offer = j.offers_of(j.bidder)[-1]
    assert offer.status == OfferStatus.REJECTED
    assert j.item_status() == ItemStatus.PUBLISHED  # back in the catalogue
    assert any("rechaz" in t.lower() for t in j.inbox(j.bidder))
    assert j.tx() is None and not j.contact_leaked()


def test_offer_without_answer_expires_after_24h_and_both_are_told(j, wa_client):
    j.offer()
    offer = j.offers_of(j.bidder)[-1]
    before_owner, before_bidder = len(j.inbox(j.owner)), len(j.inbox(j.bidder))
    # 23h later nothing happens...
    now = utcnow()
    j.db.execute(
        update(Offer)
        .where(Offer.id == offer.id)
        .values(created_at=now - timedelta(hours=23), expires_at=now + timedelta(hours=1))
    )
    j.db.commit()
    assert run_job(_job("expire_pending_offers")).affected == 0
    # ...after 24h the real scheduler job expires it.
    j.db.execute(
        update(Offer)
        .where(Offer.id == offer.id)
        .values(created_at=now - timedelta(hours=24, seconds=1), expires_at=now - timedelta(seconds=1))
    )
    j.db.commit()
    out = run_job(_job("expire_pending_offers"), "worker-e2e")
    assert out.status == JobRunStatus.SUCCEEDED and out.affected == 1
    dispatch_outbox(wa_client)
    assert j.fresh(Offer, offer.id).status == OfferStatus.EXPIRED
    assert j.item_status() == ItemStatus.PUBLISHED
    assert len(j.inbox(j.owner)) > before_owner and len(j.inbox(j.bidder)) > before_bidder  # both notified
    assert j.tx() is None and not j.contact_leaked()


def test_accepted_but_not_paid_within_24h(j, wa_client, gateway):
    j.offer()
    j.accept()
    tx = j.tx()
    assert tx.status == TransactionStatus.PAYMENT_PENDING
    assert "3.000" in [t for t in j.inbox(j.bidder) if "aceptó tu oferta" in t][0]
    j.db.execute(
        update(Transaction).where(Transaction.id == tx.id).values(payment_due_at=utcnow() - timedelta(minutes=1))
    )
    j.db.commit()
    out = run_job(_job("expire_payment_windows", gateway))  # asks Mercado Pago first: nothing was paid
    assert out.affected == 1
    dispatch_outbox(wa_client)
    assert j.fresh(Transaction, tx.id).status == TransactionStatus.EXPIRED
    assert j.item_status() == ItemStatus.PUBLISHED
    assert j.released_messages() == 0 and not j.contact_leaked()


def test_fake_payments_never_release_contacts(j, client, gateway):
    j.offer()
    j.accept()
    pay = j.pay_link()
    tx = j.tx()

    # 1) The user claims to have paid: only the provider's word counts.
    tools = j.bidder_says("ya pagué, liberá los datos", tool("get_payment_status"), say("Todavía no figura."))
    assert '"paid": false' in tools[0]["content"]
    # 2) Forged webhook (wrong secret) -> 401, recorded as a security event.
    real = gateway.simulate_payment(pay.external_reference)
    assert j.mp_webhook(real.provider_payment_id, signature_secret="not-the-secret").status_code == 401
    gateway.payments.clear()  # that "payment" never existed at the provider
    # 3) Correctly signed webhook for a payment id Mercado Pago does not know.
    assert j.mp_webhook("123456789").status_code == 200
    # 4) A real payment for the wrong amount / currency / not approved.
    wrong_amount = gateway.simulate_payment(pay.external_reference, amount=1)
    wrong_currency = gateway.simulate_payment(pay.external_reference, currency="USD")
    rejected = gateway.simulate_payment(pay.external_reference, status="rejected")
    pending = gateway.simulate_payment(pay.external_reference, status="in_process")
    for p in (wrong_amount, wrong_currency, rejected, pending):
        assert j.mp_webhook(p.provider_payment_id).status_code == 200

    assert j.fresh(Transaction, tx.id).status == TransactionStatus.PAYMENT_PENDING
    assert j.released_messages() == 0 and not j.contact_leaked()
    assert j.item_status() == ItemStatus.RESERVED


def test_duplicate_webhooks_release_exactly_once(j, wa_client, gateway, wa_post):
    # Duplicate WhatsApp delivery of the same message id: processed once.
    payload = P.text(j.bidder.test_wa_id, "busco bici", msg_id="wamid.DUPLICATE-1")[0]
    j.bot.dolphin.script = [say("¿Qué rodado?")]
    wa_post(payload)
    wa_post(payload)
    stats = process_inbound_queue(dolphin=j.bot.dolphin, wa=wa_client, gateway=gateway)
    assert stats.processed == 1

    j.offer()
    j.accept()
    pay = j.pay_link()
    pp = gateway.simulate_payment(pay.external_reference)
    assert j.mp_webhook(pp.provider_payment_id, "req-same").status_code == 200
    assert j.mp_webhook(pp.provider_payment_id, "req-same").status_code == 200  # replayed delivery
    assert j.mp_webhook(pp.provider_payment_id, "req-other").status_code == 200  # same payment, new delivery id
    assert dispatch_outbox(wa_client).sent == 2  # one message to each party, no more
    assert dispatch_outbox(wa_client).total == 0
    tx = j.tx()
    assert tx.status == TransactionStatus.CONTACT_RELEASED
    assert j.released_messages() == 2
    assert sum(tx.security_keyword in t for t in j.inbox(j.bidder)) == 1
    assert sum(tx.security_keyword in t for t in j.inbox(j.owner)) == 1


def test_second_offer_is_refused_while_the_item_is_paused(j):
    j.offer()
    other = j.make_user("Carla")
    j.bidder_says("hola", say("¡Hola!"), user=other)
    j.bot.turn(
        P.text(other.test_wa_id, f"ofrezco 260 mil por {j.code}")[0],
        tool("create_offer", item_id=j.code, offer_type="MONEY", offered_price="260000"),
        say("No se pudo."),
    )
    assert j.offers_of(other) == []
    pending = j.db.execute(
        select(func.count()).select_from(Offer).where(Offer.status == OfferStatus.PENDING)
    ).scalar_one()
    assert pending == 1
    # Once the owner rejects the first one the item is offerable again and the second bidder can offer.
    j.owner_says("RECHAZAR", tool("reject_offer"), say("Rechazada."))
    j.bot.turn(
        P.text(other.test_wa_id, f"ofrezco 260 mil por {j.code}")[0],
        tool("create_offer", item_id=j.code, offer_type="MONEY", offered_price="260000"),
        say("Oferta enviada."),
    )
    assert [o.status for o in j.offers_of(other)] == [OfferStatus.PENDING]
    assert j.item_status() == ItemStatus.RESERVED


def test_cancellations(j, gateway, wa_client):
    # a) The bidder withdraws a pending offer.
    j.offer()
    j.bidder_says("cancelo la oferta", tool("cancel_offer"), say("Cancelada."))
    assert j.offers_of(j.bidder)[-1].status == OfferStatus.CANCELLED
    assert j.item_status() == ItemStatus.PUBLISHED

    # b) The owner backs out after accepting, before the bidder pays (allowed).
    j.offer(price="235000")
    j.accept()
    pay = j.pay_link()
    j.owner_says("me arrepentí, cancelo", tool("cancel_offer"), say("Cancelaste la operación."))
    assert j.tx().status == TransactionStatus.CANCELLED
    assert j.fresh(Payment, pay.id).status == PaymentStatus.CANCELLED
    assert j.item_status() == ItemStatus.PUBLISHED
    assert j.released_messages() == 0

    # c) After the bidder paid the owner can no longer back out.
    j.offer(price="240000")
    j.accept()
    pay = j.pay_link()
    pp = gateway.simulate_payment(pay.external_reference)
    assert j.mp_webhook(pp.provider_payment_id).status_code == 200
    tools = j.owner_says("cancelo", tool("cancel_offer"), say("Ya está pago."))
    assert '"ok": false' in tools[0]["content"]
    assert j.tx().status == TransactionStatus.CONTACT_RELEASED
    assert j.item_status() == ItemStatus.COMPLETED


# ---- anti-evasion ----------------------------------------------------------------------------------------------
def test_first_evasion_warns_second_suspends_for_admin_review(j, client):
    j.offer()
    j.bidder_says("pasame tu whatsapp, el mio es 11 5555 4444")  # never reaches Dolphin
    assert j.inbox(j.bidder)[-1] == templates.EVASION_WARNING
    bidder = j.fresh(User, j.bidder.id)
    assert bidder.status == UserStatus.ACTIVE and bidder.evasion_strikes == 1
    assert j.db.execute(select(func.count()).select_from(Warning).where(Warning.user_id == bidder.id)).scalar_one() == 1
    assert "11 5555 4444" not in " ".join(j.inbox(j.owner))  # the blocked text was never relayed

    j.bidder_says("bueno, buscame en instagram @bruno.bici")
    assert j.inbox(j.bidder)[-1] == templates.EVASION_SUSPENDED
    assert j.fresh(User, j.bidder.id).status == UserStatus.SUSPENDED
    assert j.offers_of(j.bidder)[-1].status == OfferStatus.CANCELLED
    assert j.item_status() == ItemStatus.PUBLISHED  # the owner's item is not left paused

    # The case shows up in the admin console, which decides.
    h = _admin_token(j.db, client)
    bans = client.get("/admin/security/bans", headers=h).json()
    assert [(b["user_id"], b["status"]) for b in bans] == [(j.bidder.id, "PENDING_REVIEW")]
    detail = client.get(f"/admin/users/{j.bidder.id}", headers=h).json()
    assert detail["evasion_strikes"] == 2 and j.bidder.test_wa_id not in str(detail)  # masked number only
    assert (
        client.post(f"/admin/users/{j.bidder.id}/ban", json={"notes": "evasión reiterada"}, headers=h).status_code
        == 200
    )
    assert j.fresh(User, j.bidder.id).status == UserStatus.BANNED
    assert j.db.execute(select(Ban.status).where(Ban.user_id == j.bidder.id)).scalar_one() == BanStatus.CONFIRMED

    j.bidder_says("hola?")  # fixed reply, no AI
    assert j.inbox(j.bidder)[-1] == templates.ACCOUNT_SUSPENDED


# ---- integration failures ---------------------------------------------------------------------------------------
def test_whatsapp_send_error_is_retried_not_lost(j, wa_client):
    wa_client.fail_next.append(WhatsAppError("busy", code="WA_ERROR", http_status=503, retryable=True))
    j.offer()  # the replies of this turn hit the failure once
    pending = (
        j.db.execute(select(Notification).where(Notification.status == "PENDING", Notification.attempts >= 1))
        .scalars()
        .all()
    )
    assert pending, "the failed message must stay queued for retry"
    later = max(n.next_attempt_at for n in pending) + timedelta(seconds=1)
    assert dispatch_outbox(wa_client, now=later).sent >= 1
    j.db.expire_all()
    assert (
        j.db.execute(
            select(func.count()).select_from(Notification).where(Notification.status == "PENDING")
        ).scalar_one()
        == 0
    )
    assert any("Tu oferta vence en 24h" in t for t in j.inbox(j.bidder))


def test_mercadopago_errors_are_safe_and_recoverable(j, gateway, wa_client):
    j.offer()
    j.accept()
    tx = j.tx()

    # Link creation fails: the user is told, nothing is half-created, and asking again later works.
    gateway.fail = True
    tools = j.bidder_says("PAGAR", tool("create_payment"), say("Probá de nuevo en un rato."))
    assert '"ok": false' in tools[0]["content"]
    j.db.expire_all()
    assert j.db.execute(select(Payment).where(Payment.status != PaymentStatus.CREATED)).first() is None
    gateway.fail = False
    pay = j.pay_link()
    assert pay is not None and pay.status == PaymentStatus.CREATED

    # Provider down while the payment webhook arrives: 502 so Mercado Pago retries; no decision on ambiguity.
    pp = gateway.simulate_payment(pay.external_reference)
    gateway.fail = True
    assert j.mp_webhook(pp.provider_payment_id, "req-down").status_code == 502
    assert j.fresh(Transaction, tx.id).status == TransactionStatus.PAYMENT_PENDING
    # The payment window passes during the outage: the expiry job refuses to cancel without asking the provider.
    j.db.execute(
        update(Transaction).where(Transaction.id == tx.id).values(payment_due_at=utcnow() - timedelta(minutes=1))
    )
    j.db.commit()
    run_job(_job("expire_payment_windows", gateway))
    assert j.fresh(Transaction, tx.id).status == TransactionStatus.PAYMENT_PENDING

    # Provider back: Mercado Pago's retry (same delivery) is processed and both get the contacts once.
    gateway.fail = False
    assert j.mp_webhook(pp.provider_payment_id, "req-down").status_code == 200
    dispatch_outbox(wa_client)
    assert j.fresh(Transaction, tx.id).status == TransactionStatus.CONTACT_RELEASED
    assert j.released_messages() == 2


def test_dolphin_down_then_back(j, wa_post, wa_client):
    j.bot.dolphin.unavailable = True
    assert wa_post(P.text(j.bidder.test_wa_id, f"ofrezco 230 mil por {j.code}")[0])["received"] == 1
    process_inbound_queue(dolphin=j.bot.dolphin, wa=wa_client)
    dispatch_outbox(wa_client)
    assert j.inbox(j.bidder)[-1] == DOLPHIN_DOWN_REPLY
    assert j.offers_of(j.bidder) == [] and j.item_status() == ItemStatus.PUBLISHED  # nothing guessed
    j.bot.dolphin.unavailable = False
    j.offer()
    assert [o.status for o in j.offers_of(j.bidder)] == [OfferStatus.PENDING]


def test_server_restart_recovery(j, wa_post, wa_client, gateway):
    admin = create_admin(j.db, "root", "root-password-123456", AdminRole.ADMIN)
    j.db.commit()
    post_id = channel.register_manual_post(j.db, admin.id, j.code, external_ref="channel-msg-9").id
    j.db.commit()
    j.offer()
    j.accept()
    pay = j.pay_link()

    # The server goes down: a WhatsApp message is accepted (persisted) but never processed, and the bidder pays
    # at Mercado Pago while we are offline (the webhook never reaches us).
    assert wa_post(P.text(j.owner.test_wa_id, "¿ya pagó?")[0])["received"] == 1
    gateway.simulate_payment(pay.external_reference)

    # "Restart": every pooled connection is dropped, as in a fresh process; state lives only in PostgreSQL.
    j.db.close()
    dbmod.reset_engine()

    # The worker comes back: pending inbound messages are processed and the reconciliation job finds the payment.
    j.bot.dolphin.script = [tool("get_transaction"), say("Todavía no vemos el pago.")]
    assert process_inbound_queue(dolphin=j.bot.dolphin, wa=wa_client, gateway=gateway).processed == 1
    out = run_job(_job("reconcile_pending_payments", gateway), "worker-after-restart")
    assert out.status == JobRunStatus.SUCCEEDED and out.affected >= 1
    dispatch_outbox(wa_client)

    tx = j.tx()
    assert tx.status == TransactionStatus.CONTACT_RELEASED
    assert j.released_messages() == 2
    assert j.owner.test_wa_id in j.inbox(j.bidder)[-1] and tx.security_keyword in j.inbox(j.bidder)[-1]
    assert j.item_status() == ItemStatus.COMPLETED
    assert j.fresh(ChannelPost, post_id).status == ChannelPostStatus.REMOVAL_PENDING
    # Running everything again is a no-op (idempotent recovery).
    assert run_job(_job("reconcile_pending_payments", gateway)).affected == 0
    assert dispatch_outbox(wa_client).total == 0


def test_channel_reconciliation_catches_missed_events(j, client):
    h = _admin_token(j.db, client)
    r = client.post("/admin/channel/posts", json={"item_id": j.code, "external_ref": "channel-msg-1"}, headers=h)
    assert r.status_code == 201, r.text
    post_id = r.json()["id"]

    # A missed event: the item closes without the channel being told (e.g. crash between steps).
    j.db.execute(update(Item).where(Item.id == j.item_id).values(status=ItemStatus.EXPIRED))
    j.db.commit()
    assert client.get("/admin/channel/removals", headers=h).json()["closed_but_visible_unflagged"]

    # Scheduled reconciliation (same code as the admin "Reconciliar ahora" button) flags it for removal.
    out = run_job(_job("channel_reconciliation"), "worker-e2e")
    assert out.status == JobRunStatus.SUCCEEDED and out.affected >= 1
    removals = client.get("/admin/channel/removals", headers=h).json()
    assert [p["id"] for p in removals["removal_pending"]] == [post_id]
    assert removals["closed_but_visible_unflagged"] == []

    # The admin deletes it from the WhatsApp Channel by hand and confirms in the console.
    assert client.post(f"/admin/channel/posts/{post_id}/removed", headers=h).json()["status"] == "REMOVED"
    assert client.post("/admin/channel/reconcile", headers=h).json()["fixed"]["closed_items_flagged"] == 0
