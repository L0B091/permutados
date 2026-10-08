"""End-to-end happy path through the conversation layer (Dolphin mocked deterministically), the real
domain services, real PostgreSQL/Redis, the fake Mercado Pago gateway and the signed webhook endpoint."""

import json
import re
import time

from sqlalchemy import select

from app.core import db as dbmod
from app.models.channel import ChannelPost
from app.models.enums import AdminRole, ChannelPostStatus, ItemChannelStatus, ItemStatus, OfferStatus, TransactionStatus
from app.models.item import Item
from app.models.offer import Offer, Transaction
from app.models.payment import Payment
from app.services.admin.auth import create_admin
from app.services.channel import service as channel
from app.services.conversation.service import get_active_session, handle_inbound
from app.services.dolphin.mock import MockDolphinClient, say, tool
from app.services.dolphin.orchestrator import DOLPHIN_DOWN_REPLY
from app.services.mercadopago.signature import compute_signature
from app.services.users.pii import reveal_notification_body
from tests.conftest import make_image

_mid = iter(range(1, 1_000_000))


def send(db, user, text=None, script=(), images=None, gateway=None):
    client = MockDolphinClient(list(script))
    r = handle_inbound(
        db,
        wa_id=user.test_wa_id,
        text=text,
        images=images,
        image_mime="image/jpeg" if images else None,
        client=client,
        gateway=gateway,
        provider_message_id=f"wamid.{next(_mid)}",
    )
    db.commit()
    tool_results = [
        json.loads(m["content"]) for req in client.requests[-1:] for m in req["messages"] if m["role"] == "tool"
    ]
    return r, tool_results


def test_full_flow_publish_search_offer_accept_pay_release(db, make_user, publish_item, gateway, client, notifications):
    owner, bidder = make_user("Ana"), make_user("Beto")
    for i in range(6):  # catalogue noise from other owners so the search has to page
        publish_item(make_user(), title=f"Bicicleta urbana {i}", price=str(100000 + i * 1000))

    # 1) Owner sends a photo -> Dolphin identifies it and creates a DRAFT
    r, out = send(
        db,
        owner,
        images=[make_image(seed=999)],
        script=[
            tool(
                "create_publication", title="Bicicleta Trek Marlin", category="bicicletas", brand="Trek", confidence=0.8
            ),
            say("Parece una bicicleta Trek Marlin. ¿Es correcto?"),
        ],
    )
    assert out[0]["ok"] and out[0]["status"] == "DRAFT"
    code = out[0]["item_id"]

    # 2) Owner confirms and gives the data; Dolphin fills the draft and shows the preview
    r, out = send(
        db,
        owner,
        "Sí. Palermo, 250 mil, acepto permuta, sin daños y funciona perfecto",
        script=[
            tool(
                "update_publication",
                item_id=code,
                identification_confirmed=True,
                zone="Palermo",
                price="250000",
                accepts_swap=True,
                has_damage=False,
                works=True,
            ),
            tool("get_publication", item_id=code),
            say("Así queda tu publicación. ¿La publico?"),
        ],
    )
    assert out[0]["ok"] and "preview" in out[1]
    assert "250.000" in out[1]["preview"]

    # 3) Owner confirms -> PUBLISHED
    r, out = send(
        db, owner, "Sí, publicala", script=[tool("update_publication", item_id=code, publish=True), say("¡Publicada!")]
    )
    assert out[0]["status"] == "PUBLISHED"
    item = db.execute(select(Item).where(Item.public_id == code)).scalar_one()

    # Admin posts it manually to the WhatsApp Channel and registers the post
    with dbmod.session_scope() as s:
        admin = create_admin(s, "root", "root-password-123456", AdminRole.ADMIN)
        admin_id = admin.id
    post = channel.register_manual_post(db, admin_id, code, external_ref="channel-msg-1")
    db.commit()

    # 4) Bidder searches: 5 results per page, then "más"
    r, out = send(
        db,
        bidder,
        "busco bicicleta",
        script=[tool("search_publications", query="bicicleta"), say("Encontré estas opciones")],
    )
    assert len(out[0]["results"]) == 5
    r, _ = send(db, bidder, "más")  # deterministic paging, no Dolphin call needed
    assert r.replies and "ITEM-" in r.replies[0]

    # 5) Bidder selects by code and offers
    r, out = send(
        db,
        bidder,
        f"me interesa {code}",
        script=[tool("select_publication", reference=code), say("Bicicleta Trek Marlin, Palermo")],
    )
    assert out[0]["item_id"] == code
    r, out = send(
        db,
        bidder,
        "ofrezco 230 mil",
        script=[tool("create_offer", offer_type="MONEY", offered_price="230000"), say("Oferta enviada.")],
    )
    offer_code = out[0]["offer_id"]
    assert "Tu oferta vence en 24h" in out[0]["message_for_user"]
    owner_note = notifications(user_id=owner.id, kind="OFFER_RECEIVED_OWNER")[0].body
    assert "Tenés 24h para aceptar o rechazar, luego la oferta caduca" in owner_note
    db.refresh(item)
    assert item.status == ItemStatus.RESERVED  # paused: hidden while the offer is pending

    # 6) Owner accepts
    r, out = send(db, owner, "acepto la oferta", script=[tool("accept_offer"), say("Aceptada.")])
    assert out[0]["ok"] and out[0]["status"] == "PAYMENT_PENDING"
    tx_code = out[0]["transaction_id"]
    accepted_note = notifications(user_id=bidder.id, kind="OFFER_ACCEPTED_BIDDER")[0].body
    assert "3.000" in accepted_note and "24" in accepted_note

    # 7) Bidder asks for the payment link; "ya pagué" does not change anything
    r, out = send(db, bidder, "quiero pagar", gateway=gateway, script=[tool("create_payment"), say("Link")])
    assert out[0]["checkout_url"].startswith("https://")
    r, out = send(db, bidder, "ya pagué", gateway=gateway, script=[tool("get_payment_status"), say("…")])
    assert out[0]["paid"] is False

    # 8) Mercado Pago confirms through a signed webhook -> contact released
    tx = db.execute(select(Transaction).where(Transaction.public_id == tx_code)).scalar_one()
    pay = db.execute(select(Payment).where(Payment.transaction_id == tx.id)).scalar_one()
    pp = gateway.simulate_payment(pay.external_reference)
    ts = str(int(time.time()))
    sig = compute_signature("test-mp-webhook-secret", pp.provider_payment_id, "req-e2e", ts)
    resp = client.post(
        f"/webhooks/mercadopago?data.id={pp.provider_payment_id}&type=payment",
        json={"type": "payment", "data": {"id": pp.provider_payment_id}},
        headers={"x-signature": f"ts={ts},v1={sig}", "x-request-id": "req-e2e"},
    )
    assert resp.status_code == 200

    db.expire_all()
    tx = db.get(Transaction, tx.id)
    assert tx.status == TransactionStatus.CONTACT_RELEASED
    assert re.fullmatch(r"[A-Z]+-\d{4}", tx.security_keyword)
    assert (
        db.execute(select(Offer).where(Offer.public_id == offer_code)).scalar_one().status
        == OfferStatus.CONTACT_RELEASED
    )
    to_bidder = reveal_notification_body(notifications(user_id=bidder.id, kind="CONTACT_RELEASED_BIDDER")[0])
    to_owner = reveal_notification_body(notifications(user_id=owner.id, kind="CONTACT_RELEASED_OWNER")[0])
    assert owner.test_wa_id in to_bidder and bidder.test_wa_id in to_owner
    assert tx.security_keyword in to_bidder and tx.security_keyword in to_owner
    assert db.get(Item, item.id).status == ItemStatus.COMPLETED
    assert db.get(ChannelPost, post.id).status == ChannelPostStatus.REMOVAL_PENDING

    # channel updated -> the admin sees the removal, deletes the post by hand and confirms -> operation finished
    pending = channel.removals_needed(db)["removal_pending"]
    assert [p["id"] for p in pending] == [post.id]
    channel.confirm_removed(db, admin_id, post.id)
    db.commit()
    db.expire_all()
    assert db.get(ChannelPost, post.id).status == ChannelPostStatus.REMOVED
    done = db.get(Item, item.id)
    assert done.status == ItemStatus.COMPLETED and done.channel_status == ItemChannelStatus.REMOVED
    assert channel.removals_needed(db)["removal_pending"] == []
    assert db.get(Transaction, tx.id).status == TransactionStatus.CONTACT_RELEASED  # terminal

    # the completed item no longer shows up in searches
    other = make_user()
    r, out = send(db, other, "busco trek", script=[tool("search_publications", query="trek marlin"), say("x")])
    assert all(res["item_id"] != code for res in out[0]["results"])


def test_dolphin_down_keeps_state_and_answers_safely(db, make_user, publish_item):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    r = handle_inbound(
        db,
        wa_id=bidder.test_wa_id,
        text=f"ofrezco 100 mil por {item.public_id}",
        client=MockDolphinClient(unavailable=True),
        provider_message_id="wamid.down",
    )
    db.commit()
    assert r.replies == [DOLPHIN_DOWN_REPLY]
    assert db.execute(select(Offer)).first() is None
    assert get_active_session(db, bidder.id) is not None
    db.refresh(item)
    assert item.status == ItemStatus.PUBLISHED
