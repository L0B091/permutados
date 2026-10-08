from datetime import timedelta

from app.core.clock import utcnow
from app.models.offer import Offer
from app.services.conversation import state_machine as sm
from app.services.conversation.service import get_active_session, handle_inbound
from app.services.dolphin.context import build_context
from app.services.dolphin.mock import MockDolphinClient, say, tool
from app.services.offers import service as offers


def test_illegal_transitions_are_refused(db, make_user):
    u = make_user()
    conv = get_active_session(db, u.id)
    assert conv.conversation_state == "IDLE"
    assert not sm.transition(conv, sm.State.PUBLISHING_PREVIEW, reason="test")  # preview needs a draft flow
    assert conv.conversation_state == "IDLE"
    assert sm.transition(conv, sm.State.BROWSING_RESULTS, reason="test")
    assert sm.transition(conv, sm.State.ITEM_SELECTED, reason="test")
    db.rollback()


def test_failed_tools_do_not_move_state(db, make_user):
    u = make_user()
    d = MockDolphinClient([tool("select_publication", reference="ITEM-ZZZZZ"), say("No lo encontré")])
    handle_inbound(db, wa_id=u.test_wa_id, text="quiero el ITEM-ZZZZZ", client=d, provider_message_id="m1")
    db.commit()
    assert get_active_session(db, u.id).conversation_state == "IDLE"


def test_reconcile_follows_database_truth(db, make_user, publish_item):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    d = MockDolphinClient(
        [
            tool("select_publication", reference=item.public_id),
            tool("create_offer", offer_type="MONEY", offered_price="100000"),
            say("Enviada"),
        ]
    )
    handle_inbound(
        db, wa_id=bidder.test_wa_id, text=f"ofrezco 100 mil por {item.public_id}", client=d, provider_message_id="m1"
    )
    db.commit()
    bconv = get_active_session(db, bidder.id)
    assert bconv.conversation_state == "OFFER_PENDING"
    # The owner is told about the offer: their next turn starts in OWNER_DECIDING.
    oconv = get_active_session(db, owner.id)
    assert sm.reconcile(db, owner, oconv) == sm.State.OWNER_DECIDING
    ctx = build_context(db, owner, oconv)
    assert "aceptar o rechazar" in ctx["expected_next"] and ctx["pending_offers_to_answer"]
    db.commit()
    # Owner accepts outside the conversation -> bidder's state moves to PAYMENT_PENDING on reconcile.
    offer = db.query(Offer).one()
    offers.accept_offer(db, owner, offer.public_id)
    db.commit()
    bconv = get_active_session(db, bidder.id)
    assert sm.reconcile(db, bidder, bconv) == sm.State.PAYMENT_PENDING and bconv.transaction_id
    assert sm.reconcile(db, owner, get_active_session(db, owner.id)) == sm.State.IDLE
    db.commit()


def test_reconcile_after_offer_expired(db, make_user, publish_item):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    conv = get_active_session(db, bidder.id)
    conv.conversation_state, conv.current_offer_id = "OFFER_PENDING", res.offer.id
    db.commit()
    offers.expire_pending_offers(db, now=utcnow() + timedelta(hours=25))
    db.commit()
    assert sm.reconcile(db, bidder, get_active_session(db, bidder.id)) == sm.State.IDLE
    db.rollback()


def test_intent_and_expected_next_reach_dolphin_as_data(db, make_user):
    u = make_user()
    d = MockDolphinClient([say("¿Qué buscás?")])
    handle_inbound(db, wa_id=u.test_wa_id, text="busco una heladera", client=d, provider_message_id="m1")
    ctx_msg = d.requests[0]["messages"][1]
    assert ctx_msg["role"] == "system" and ctx_msg["content"].startswith("BACKEND_CONTEXT")
    assert '"current_intent": "SEARCH"' in ctx_msg["content"] and '"expected_next"' in ctx_msg["content"]
