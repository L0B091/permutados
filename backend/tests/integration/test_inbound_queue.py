"""Signed WhatsApp webhook -> durable queue -> worker -> conversation engine -> outbox -> WhatsApp (mock)."""

import threading
from datetime import timedelta

from sqlalchemy import select, text

from app.core import db as dbmod
from app.core.clock import utcnow
from app.models.conversation import Message
from app.models.enums import MessageDirection
from app.models.payment import WebhookEvent
from app.models.user import User
from app.services.conversation import inbound_queue
from app.services.conversation.inbound_queue import process_inbound_queue
from app.services.dolphin.mock import MockDolphinClient, say, tool
from app.services.notifications import templates
from app.services.notifications.outbox import dispatch_outbox
from app.services.whatsapp.client import MediaFile, WhatsAppError
from tests.conftest import make_image
from tests.helpers import wa_payloads as P

WA = "5491160000001"


def events():
    with dbmod.session_scope() as s:
        return (
            s.execute(select(WebhookEvent).where(WebhookEvent.provider == "whatsapp").order_by(WebhookEvent.id))
            .scalars()
            .all()
        )


def test_text_message_round_trip(wa_post, wa_client):
    payload, mid = P.text(WA, "hola", name="Ana")
    assert wa_post(payload)["received"] == 1
    dolphin = MockDolphinClient([say("¡Hola Ana! ¿Querés publicar o buscar algo?")])
    stats = process_inbound_queue(dolphin=dolphin, wa=wa_client)
    assert stats.processed == 1
    [ev] = events()
    assert ev.status == "PROCESSED" and "env" not in ev.payload and "identity" not in ev.payload
    assert wa_client.read == [mid]  # read receipt
    assert dispatch_outbox(wa_client).sent == 1
    assert wa_client.texts_to(WA) == ["¡Hola Ana! ¿Querés publicar o buscar algo?"]
    # Dolphin got the user text only in a user-role message
    msgs = dolphin.requests[0]["messages"]
    assert msgs[-1] == {"role": "user", "content": "hola"} and all("hola" != m["content"] for m in msgs[:-1])


def test_duplicate_delivery_is_processed_once(wa_post, wa_client):
    payload, _ = P.text(WA, "hola")
    wa_post(payload)
    assert wa_post(payload)["received"] == 0
    process_inbound_queue(dolphin=MockDolphinClient([say("ok")]), wa=wa_client)
    assert dispatch_outbox(wa_client).sent == 1


def test_image_is_downloaded_validated_and_identified(wa_post, wa_client, db):
    wa_client.media["MEDIA1"] = MediaFile(make_image(seed=5), "image/jpeg")
    wa_post(P.image(WA, "MEDIA1")[0])
    dolphin = MockDolphinClient(
        [
            tool("create_publication", title="Taladro Bosch", category="herramientas"),
            say("Parece un taladro Bosch. ¿Es correcto?"),
        ]
    )
    assert process_inbound_queue(dolphin=dolphin, wa=wa_client).processed == 1
    user_msg = dolphin.requests[0]["messages"][-1]["content"]
    assert user_msg[1]["type"] == "image_url" and user_msg[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    tool_out = [m for m in dolphin.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert '"status": "DRAFT"' in tool_out
    from app.services.conversation.service import get_active_session
    from app.services.users.service import find_by_whatsapp

    conv = get_active_session(db, find_by_whatsapp(db, WA).id)
    assert conv.conversation_state == "PUBLISHING_CONFIRM_IDENTIFICATION" and conv.current_intent == "PUBLISH"
    db.rollback()


def test_invalid_image_is_rejected_before_dolphin(wa_post, wa_client):
    wa_client.media["BAD"] = MediaFile(b"not an image at all" * 200, "image/jpeg")
    wa_post(P.image(WA, "BAD")[0])
    dolphin = MockDolphinClient([say("no debería llamarse")])
    process_inbound_queue(dolphin=dolphin, wa=wa_client)
    assert dolphin.requests == []
    dispatch_outbox(wa_client)
    assert len(wa_client.texts_to(WA)) == 1


def test_media_download_retried_then_user_asked_to_resend(wa_post, wa_client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "inbound_max_attempts", 3)
    wa_post(P.image(WA, "MISSING")[0])
    for _ in range(2):
        wa_client.fail_next.append(WhatsAppError("down", retryable=True))
    dolphin = MockDolphinClient()
    now = utcnow()
    assert process_inbound_queue(dolphin=dolphin, wa=wa_client, now=now).retried == 1
    [ev] = events()
    assert ev.status == "RECEIVED" and ev.attempts == 1 and ev.next_attempt_at > now
    assert process_inbound_queue(dolphin=dolphin, wa=wa_client, now=now).processed == 0  # not due yet
    later = ev.next_attempt_at + timedelta(seconds=1)
    assert process_inbound_queue(dolphin=dolphin, wa=wa_client, now=later).retried == 1
    later = events()[0].next_attempt_at + timedelta(seconds=1)
    assert process_inbound_queue(dolphin=dolphin, wa=wa_client, now=later).processed == 1  # gives up: asks again
    dispatch_outbox(wa_client)
    assert wa_client.texts_to(WA) == [templates.IMAGE_DOWNLOAD_FAILED]


def test_contact_card_location_and_audio(wa_post, wa_client, db):
    wa_post(P.location(WA)[0])
    wa_post(P.audio(WA)[0])
    wa_post(P.contacts(WA, "+54 9 11 4444-3333")[0])
    process_inbound_queue(dolphin=MockDolphinClient(), wa=wa_client)
    dispatch_outbox(wa_client)
    texts = wa_client.texts_to(WA)
    assert texts[0] == templates.LOCATION_NOT_ALLOWED and texts[1] == templates.UNSUPPORTED_MESSAGE
    assert texts[2] == templates.EVASION_WARNING
    user = db.execute(select(User)).scalar_one()
    assert user.evasion_strikes == 1
    blocked = db.execute(select(Message).where(Message.blocked.is_(True))).scalar_one()
    assert blocked.body is None and blocked.body_enc and "4444" not in blocked.body_enc


def test_poison_event_does_not_block_the_queue(wa_post, wa_client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "inbound_max_attempts", 2)
    wa_post(P.text(WA, "rompe")[0])
    wa_post(P.text("5491160000002", "hola")[0])
    real = inbound_queue.handle_inbound

    def flaky(session, **kw):
        if kw.get("text") == "rompe":
            raise RuntimeError("bug")
        return real(session, **kw)

    monkeypatch.setattr(inbound_queue, "handle_inbound", flaky)
    stats = process_inbound_queue(dolphin=MockDolphinClient([say("hola!")]), wa=wa_client)
    assert stats.retried == 1 and stats.processed == 1
    first = events()[0]
    process_inbound_queue(dolphin=MockDolphinClient(), wa=wa_client, now=first.next_attempt_at + timedelta(seconds=1))
    first = events()[0]
    assert first.status == "FAILED" and first.error == "internal:RuntimeError" and "env" not in first.payload


def test_messages_processed_in_order_and_single_consumer(wa_post, wa_client, db):
    for t in ("busco bici", "más", "el 1"):
        wa_post(P.text(WA, t)[0])
    seen = []

    def record(messages):
        seen.append(messages[-1]["content"])
        return say("ok")

    process_inbound_queue(dolphin=MockDolphinClient([record, record, record]), wa=wa_client)
    assert seen[0] == "busco bici" and seen[-1] == "el 1"

    # A second consumer gets nothing while the advisory lock is held by another one.
    with dbmod.get_engine().connect() as c:
        c.execute(text("SELECT pg_advisory_lock(:k)"), {"k": inbound_queue._LOCK_KEY})
        wa_post(P.text(WA, "hola")[0])
        result = []
        t = threading.Thread(
            target=lambda: result.append(process_inbound_queue(dolphin=MockDolphinClient(), wa=wa_client).processed)
        )
        t.start()
        t.join()
        assert result == [0]
        c.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": inbound_queue._LOCK_KEY})
        c.commit()
    assert process_inbound_queue(dolphin=MockDolphinClient(), wa=wa_client).processed == 1


def test_suspended_user_gets_fixed_reply_without_ai(wa_post, wa_client, db):
    from app.models.enums import UserStatus
    from app.services.users.service import get_or_create_by_whatsapp

    u = get_or_create_by_whatsapp(db, WA)
    u.status = UserStatus.SUSPENDED
    db.commit()
    wa_post(P.text(WA, "hola")[0])
    dolphin = MockDolphinClient()
    process_inbound_queue(dolphin=dolphin, wa=wa_client)
    dispatch_outbox(wa_client)
    assert dolphin.requests == [] and wa_client.texts_to(WA) == [templates.ACCOUNT_SUSPENDED]
    out = db.execute(select(Message).where(Message.direction == MessageDirection.OUTBOUND)).scalars().all()
    assert len(out) == 1


def test_worker_messaging_jobs_wiring(db, wa_post, wa_client, gateway):
    """The scheduler's messaging jobs drain the inbound queue and send the reply through the WhatsApp client."""
    from app.services.dolphin.mock import MockDolphinClient, say
    from app.workers.jobs import messaging_jobs
    from tests.helpers import wa_payloads as P

    dolphin = MockDolphinClient(script=[say("¡Hola! ¿Querés publicar o buscar algo?")])
    jobs = messaging_jobs(dolphin=dolphin, wa=wa_client, gateway=gateway)
    assert set(jobs) == {"process_inbound_queue", "dispatch_outbox"}
    wa_post(P.text("5491170000009", "hola")[0])
    assert jobs["process_inbound_queue"][0]() == 1
    assert jobs["dispatch_outbox"][0]() == 1
    assert wa_client.texts_to("5491170000009") == ["¡Hola! ¿Querés publicar o buscar algo?"]
    assert set(messaging_jobs(dolphin=dolphin, wa=None)) == {"process_inbound_queue"}
