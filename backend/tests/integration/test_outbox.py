"""Outbox dispatcher against real PostgreSQL with the recording WhatsApp client."""

import threading
from datetime import timedelta

from sqlalchemy import select

from app.core import db as dbmod
from app.core.clock import utcnow
from app.models.conversation import Message, Notification
from app.models.enums import MessageDirection, NotificationStatus
from app.models.user import User
from app.services.notifications.outbox import (
    apply_status_update,
    dispatch_outbox,
    may_send_template,
    window_open,
)
from app.services.notifications.service import SENSITIVE_PLACEHOLDER, enqueue
from app.services.whatsapp.client import RecordingWhatsAppClient, WhatsAppError


def _set_inbound(db, user, delta):
    u = db.get(User, user.id)
    u.last_inbound_at = utcnow() - delta if delta is not None else None
    db.commit()


def _n(db, nid):
    db.expire_all()
    return db.get(Notification, nid)


def test_window_math():
    now = utcnow()
    assert window_open(now - timedelta(hours=1), now)
    assert window_open(now - timedelta(hours=23, minutes=40), now)
    assert not window_open(now - timedelta(hours=23, minutes=50), now)  # 15-minute safety margin
    assert not window_open(None, now)


def test_inside_window_sends_session_text_and_records_message(db, make_user, wa_client):
    u = make_user()
    _set_inbound(db, u, timedelta(minutes=5))
    n = enqueue(db, user_id=u.id, kind="OFFER_RECEIVED_OWNER", body="📩 Recibiste una oferta", idempotency_key="k1")
    db.commit()
    stats = dispatch_outbox(wa_client)
    assert stats.sent == 1
    assert wa_client.sent == [{"type": "text", "to": u.test_wa_id, "body": "📩 Recibiste una oferta", "key": "k1"}]
    n = _n(db, n.id)
    assert n.status == NotificationStatus.SENT and n.channel == "SESSION" and n.provider_message_id
    out = db.execute(select(Message).where(Message.direction == MessageDirection.OUTBOUND)).scalar_one()
    assert out.provider_message_id == n.provider_message_id
    assert dispatch_outbox(wa_client).total == 0  # nothing sent twice


def test_outside_window_one_template_then_full_messages_when_user_writes(db, make_user, wa_client):
    from app.services.conversation.service import handle_inbound
    from app.services.dolphin.mock import MockDolphinClient, say

    u = make_user()
    _set_inbound(db, u, timedelta(hours=30))
    a = enqueue(db, user_id=u.id, kind="OFFER_RECEIVED_OWNER", body="oferta completa", idempotency_key="a")
    b = enqueue(db, user_id=u.id, kind="OFFER_CANCELLED_OWNER", body="cancelada", idempotency_key="b")
    db.commit()
    stats = dispatch_outbox(wa_client)
    assert stats.templated == 1 and stats.awaiting == 2
    [tpl] = wa_client.sent
    assert tpl["type"] == "template" and tpl["name"] == "permutados_novedad" and tpl["language"] == "es_AR"
    assert tpl["params"] == ["recibiste una oferta: tenés 24h para aceptar o rechazar"]
    assert _n(db, a.id).status == _n(db, b.id).status == NotificationStatus.AWAITING_WINDOW
    assert dispatch_outbox(wa_client).total == 0  # waiting, no template spam

    handle_inbound(
        db, wa_id=u.test_wa_id, text="hola", client=MockDolphinClient([say("¡Hola!")]), provider_message_id="wamid.in1"
    )
    db.commit()
    stats = dispatch_outbox(wa_client)
    assert stats.sent == 3  # the two queued messages + the reply to "hola"
    assert wa_client.texts_to(u.test_wa_id) == ["oferta completa", "cancelada", "¡Hola!"]


def test_template_rate_limited_per_silence_period(db, make_user):
    u = make_user()
    user = db.get(User, u.id)
    now = utcnow()
    user.last_inbound_at, user.last_template_at = now - timedelta(hours=30), None
    assert may_send_template(user, now)
    user.last_template_at = now - timedelta(hours=2)
    assert not may_send_template(user, now)
    user.last_template_at = now - timedelta(hours=25)
    assert may_send_template(user, now)
    user.last_template_at, user.last_inbound_at = now - timedelta(hours=2), now - timedelta(hours=1)
    assert may_send_template(user, now)  # the user wrote after the last template
    db.rollback()


def test_expired_notifications_are_not_delivered(db, make_user, wa_client):
    u = make_user()
    _set_inbound(db, u, timedelta(minutes=1))
    n = enqueue(
        db,
        user_id=u.id,
        kind="OFFER_RECEIVED_OWNER",
        body="Tenés 24h",
        idempotency_key="e",
        expires_at=utcnow() - timedelta(minutes=1),
    )
    db.commit()
    assert dispatch_outbox(wa_client).expired == 1
    assert _n(db, n.id).status == NotificationStatus.EXPIRED and wa_client.sent == []


def test_meta_says_window_closed_synchronously(db, make_user, wa_client):
    u = make_user()
    _set_inbound(db, u, timedelta(hours=2))
    wa_client.outside_window_for.add(u.test_wa_id)
    n = enqueue(db, user_id=u.id, kind="REPLY", body="x", idempotency_key="w")
    db.commit()
    stats = dispatch_outbox(wa_client)
    assert stats.templated == 1 and _n(db, n.id).status == NotificationStatus.AWAITING_WINDOW


def test_status_webhook_131047_falls_back_to_template(db, make_user, wa_client):
    u = make_user()
    _set_inbound(db, u, timedelta(hours=1))
    n = enqueue(db, user_id=u.id, kind="REPLY", body="x", idempotency_key="s")
    db.commit()
    dispatch_outbox(wa_client)
    mid = _n(db, n.id).provider_message_id
    assert apply_status_update(db, mid, "failed", 131047)
    db.commit()
    stats = dispatch_outbox(wa_client)
    assert stats.templated == 1 and _n(db, n.id).status == NotificationStatus.AWAITING_WINDOW
    assert apply_status_update(db, "wamid.unknown", "delivered", None) is False


def test_retry_backoff_and_permanent_failure(db, make_user, wa_client):
    u = make_user()
    _set_inbound(db, u, timedelta(minutes=1))
    n = enqueue(db, user_id=u.id, kind="REPLY", body="x", idempotency_key="r")
    db.commit()
    wa_client.fail_next.append(WhatsAppError("busy", code="WA_ERROR", http_status=503, retryable=True))
    assert dispatch_outbox(wa_client).retried == 1
    row = _n(db, n.id)
    assert row.status == NotificationStatus.PENDING and row.attempts == 1 and row.next_attempt_at > utcnow()
    assert dispatch_outbox(wa_client).total == 0  # not due yet
    assert dispatch_outbox(wa_client, now=row.next_attempt_at + timedelta(seconds=1)).sent == 1

    m = enqueue(db, user_id=u.id, kind="REPLY", body="y", idempotency_key="p")
    db.commit()
    wa_client.fail_next.append(WhatsAppError("bad number", code="WA_ERROR", meta_code=131026, http_status=400))
    assert dispatch_outbox(wa_client).failed == 1
    assert _n(db, m.id).status == NotificationStatus.FAILED and _n(db, m.id).last_error.startswith("WA_ERROR")


def test_max_attempts(db, make_user, wa_client, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "outbox_max_attempts", 2)
    u = make_user()
    _set_inbound(db, u, timedelta(minutes=1))
    n = enqueue(db, user_id=u.id, kind="REPLY", body="x", idempotency_key="m")
    db.commit()
    when = utcnow()
    for _ in range(2):
        wa_client.fail_next.append(WhatsAppError("busy", retryable=True))
        dispatch_outbox(wa_client, now=when)
        row = _n(db, n.id)
        when = (row.next_attempt_at or when) + timedelta(seconds=1)
    assert _n(db, n.id).status == NotificationStatus.FAILED


def test_sensitive_body_encrypted_at_rest_and_delivered_in_clear(db, make_user, wa_client):
    u = make_user()
    _set_inbound(db, u, timedelta(minutes=1))
    n = enqueue(
        db,
        user_id=u.id,
        kind="CONTACT_RELEASED_BIDDER",
        body="WhatsApp: +5491199990000 LUNA-1234",
        idempotency_key="sens",
        sensitive=True,
    )
    db.commit()
    row = _n(db, n.id)
    assert row.body == SENSITIVE_PLACEHOLDER and "5491199990000" not in (row.body_enc or "")
    dispatch_outbox(wa_client)
    assert wa_client.texts_to(u.test_wa_id) == ["WhatsApp: +5491199990000 LUNA-1234"]
    out = db.execute(
        select(Message).where(Message.provider_message_id == _n(db, n.id).provider_message_id)
    ).scalar_one()
    assert out.body == SENSITIVE_PLACEHOLDER


def test_concurrent_dispatchers_send_each_message_once(db, make_user):
    users = [make_user() for _ in range(3)]
    for u in users:
        _set_inbound(db, u, timedelta(minutes=1))
    for i in range(30):
        enqueue(db, user_id=users[i % 3].id, kind="REPLY", body=f"m{i}", idempotency_key=f"c{i}")
    db.commit()
    client = RecordingWhatsAppClient()
    lock = threading.Lock()
    orig = client.send_text

    def safe_send(*a, **kw):
        with lock:
            return orig(*a, **kw)

    client.send_text = safe_send  # type: ignore[method-assign]
    threads = [threading.Thread(target=dispatch_outbox, args=(client,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bodies = sorted(m["body"] for m in client.sent)
    assert bodies == sorted(f"m{i}" for i in range(30))
    with dbmod.session_scope() as s:
        assert {n.status for n in s.execute(select(Notification)).scalars()} == {NotificationStatus.SENT}
