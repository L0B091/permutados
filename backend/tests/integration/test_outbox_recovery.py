"""Crash-safety of the WhatsApp outbox: retries never duplicate a message (real PostgreSQL)."""

from datetime import timedelta

from sqlalchemy import func, select

from app.core.clock import utcnow
from app.models.conversation import Message, Notification
from app.models.enums import MessageDirection, NotificationStatus, SystemMode
from app.models.user import User
from app.services.notifications.outbox import (
    DispatchStats,
    _claim_next,
    _send,
    apply_status_update,
    dispatch_outbox,
    recover_stale_sending,
)
from app.services.notifications.service import enqueue
from app.services.system import config_service as cfg
from app.services.whatsapp.client import WhatsAppError
from tests.helpers import wa_payloads as wa


def _inbound(db, user, delta):
    u = db.get(User, user.id)
    u.last_inbound_at = utcnow() - delta
    db.commit()


def _n(db, nid):
    db.expire_all()
    return db.get(Notification, nid)


def _crash_after_send(wa_client):
    """Claim + send, then the 'process dies' before the settle transaction."""
    found, claim = _claim_next(utcnow(), set(), DispatchStats(), None)
    assert found and claim is not None
    return claim, _send(wa_client, claim)


def _outbound_count(db):
    db.expire_all()
    return db.execute(
        select(func.count()).select_from(Message).where(Message.direction == MessageDirection.OUTBOUND)
    ).scalar_one()


def test_crash_after_send_is_confirmed_by_status_webhook_without_resend(db, make_user, wa_client, wa_post):
    u = make_user()
    _inbound(db, u, timedelta(minutes=5))
    n = enqueue(db, user_id=u.id, kind="OFFER_RECEIVED_OWNER", body="hola", idempotency_key="crash-1")
    db.commit()
    claim, mid = _crash_after_send(wa_client)
    assert _n(db, n.id).status == NotificationStatus.SENDING and len(wa_client.sent) == 1

    # Meta echoes biz_opaque_callback_data (our idempotency key) in the status webhook.
    assert wa_post(wa.status(mid, "sent", u.test_wa_id, callback_data=claim.key))["statuses"] == 1
    row = _n(db, n.id)
    assert row.status == NotificationStatus.SENT and row.provider_message_id == mid
    assert "sending" not in (row.payload or {})
    assert _outbound_count(db) == 1

    # Neither the dispatcher nor the recovery job sends it again.
    assert dispatch_outbox(wa_client).total == 0
    assert recover_stale_sending(now=utcnow() + timedelta(hours=1)) == 0
    assert len(wa_client.sent) == 1


def test_crash_without_evidence_is_resent_exactly_once_after_grace(db, make_user, wa_client):
    u = make_user()
    _inbound(db, u, timedelta(minutes=5))
    n = enqueue(db, user_id=u.id, kind="REPLY", body="hola", idempotency_key="crash-2")
    db.commit()
    # The worker died after claiming but before Meta accepted the request.
    found, claim = _claim_next(utcnow(), set(), DispatchStats(), None)
    assert found and claim
    assert recover_stale_sending() == 0  # still inside the grace period: maybe in flight
    assert dispatch_outbox(wa_client).total == 0  # a SENDING row is never picked up again
    assert recover_stale_sending(now=utcnow() + timedelta(minutes=16)) == 1
    row = _n(db, n.id)
    assert row.status == NotificationStatus.PENDING and row.payload["recovered"] == 1
    assert dispatch_outbox(wa_client).sent == 1
    assert dispatch_outbox(wa_client).total == 0
    assert [m["key"] for m in wa_client.sent] == ["crash-2"]  # same idempotency key, single delivery
    assert _n(db, n.id).status == NotificationStatus.SENT


def test_template_in_doubt_is_never_resent(db, make_user, wa_client):
    u = make_user()
    _inbound(db, u, timedelta(hours=30))  # outside the 24h window -> template path
    n = enqueue(db, user_id=u.id, kind="OFFER_RECEIVED_OWNER", body="oferta", idempotency_key="tpl-1")
    db.commit()
    claim, _ = _crash_after_send(wa_client)
    assert claim.mode == "TEMPLATE" and claim.key == "tpl-1:tpl"
    assert recover_stale_sending(now=utcnow() + timedelta(minutes=16)) == 1
    row = _n(db, n.id)
    assert row.status == NotificationStatus.AWAITING_WINDOW
    assert dispatch_outbox(wa_client).total == 0
    assert [m["type"] for m in wa_client.sent] == ["template"]


def test_late_settle_after_webhook_does_not_duplicate_records(db, make_user, wa_client):
    from app.core.db import session_scope
    from app.services.notifications.outbox import _settle

    u = make_user()
    _inbound(db, u, timedelta(minutes=5))
    n = enqueue(db, user_id=u.id, kind="OFFER_RECEIVED_OWNER", body="x", idempotency_key="race-1")
    db.commit()
    claim, mid = _crash_after_send(wa_client)
    with session_scope() as s:  # webhook wins the race
        assert apply_status_update(s, mid, "delivered", None, callback_data=claim.key)
    stats = DispatchStats()
    with session_scope() as s:  # then the original worker settles
        assert _settle(s, claim, mid, None, utcnow(), stats) is False
    assert _n(db, n.id).status == NotificationStatus.SENT
    assert _outbound_count(db) == 1


def test_duplicate_delivery_is_detected(db, make_user, wa_client):
    from app.core.db import session_scope

    u = make_user()
    _inbound(db, u, timedelta(minutes=5))
    n = enqueue(db, user_id=u.id, kind="REPLY", body="x", idempotency_key="dup-1")
    db.commit()
    dispatch_outbox(wa_client)
    with session_scope() as s:
        apply_status_update(s, "wamid.other", "sent", None, callback_data="dup-1")
    row = _n(db, n.id)
    assert row.payload["duplicate_message_ids"] == ["wamid.other"]
    assert row.status == NotificationStatus.SENT


def test_failed_status_for_in_flight_message_goes_to_retry_path(db, make_user, wa_client):
    from app.core.db import session_scope

    u = make_user()
    _inbound(db, u, timedelta(minutes=5))
    n = enqueue(db, user_id=u.id, kind="REPLY", body="x", idempotency_key="f-1")
    db.commit()
    claim, mid = _crash_after_send(wa_client)
    with session_scope() as s:
        apply_status_update(s, mid, "failed", 131047, callback_data=claim.key)
    row = _n(db, n.id)
    assert row.status == NotificationStatus.PENDING and row.payload.get("window_closed") is True
    assert row.sending_started_at is None


def test_transient_error_then_success_sends_once(db, make_user, wa_client):
    u = make_user()
    _inbound(db, u, timedelta(minutes=5))
    n = enqueue(db, user_id=u.id, kind="REPLY", body="x", idempotency_key="t-1")
    db.commit()
    wa_client.fail_next.append(WhatsAppError("boom", code="WA_ERROR", retryable=True))
    assert dispatch_outbox(wa_client).retried == 1
    assert _n(db, n.id).status == NotificationStatus.PENDING
    assert dispatch_outbox(wa_client, now=utcnow() + timedelta(hours=1)).sent == 1
    assert dispatch_outbox(wa_client, now=utcnow() + timedelta(hours=2)).total == 0
    assert len(wa_client.sent) == 1


def test_emergency_holds_sensitive_notifications_but_not_regular_ones(db, make_user, wa_client):
    u = make_user()
    _inbound(db, u, timedelta(minutes=5))
    secret = enqueue(db, user_id=u.id, kind="CONTACT_RELEASED_BIDDER", body="tel", idempotency_key="s", sensitive=True)
    plain = enqueue(db, user_id=u.id, kind="REPLY", body="hola", idempotency_key="p")
    cfg.set_value(db, "system_mode", SystemMode.EMERGENCY.value)
    db.commit()
    stats = dispatch_outbox(wa_client)
    assert stats.held == 1 and stats.sent == 1
    assert _n(db, secret.id).status == NotificationStatus.PENDING
    assert _n(db, plain.id).status == NotificationStatus.SENT
    cfg.set_value(db, "system_mode", SystemMode.ONLINE.value)
    db.commit()
    assert dispatch_outbox(wa_client, now=utcnow() + timedelta(minutes=6)).sent == 1
    assert _n(db, secret.id).status == NotificationStatus.SENT
