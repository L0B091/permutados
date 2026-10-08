"""Admin endpoints added for the Android console (phase 4): real app, real PostgreSQL."""

import pytest
from sqlalchemy import select

from app.core import db as dbmod
from app.models.audit import AuditLog
from app.models.enums import AdminRole, ReportReason
from app.models.report import Report
from app.services.admin.auth import create_admin
from app.services.conversation.service import handle_inbound
from app.services.dolphin.mock import MockDolphinClient, say
from app.services.offers import service as offers
from app.services.payments import service as payments

PASSWORDS = {"root": "root-password-123456", "viewer": "viewer-password-123", "mod": "moderator-pass-1234"}


@pytest.fixture
def h(client):
    with dbmod.session_scope() as s:
        create_admin(s, "root", PASSWORDS["root"], AdminRole.ADMIN)
        create_admin(s, "viewer", PASSWORDS["viewer"], AdminRole.VIEWER)
        create_admin(s, "mod", PASSWORDS["mod"], AdminRole.MODERATOR)

    def _h(user):
        r = client.post("/admin/auth/login", json={"username": user, "password": PASSWORDS[user]})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    return _h


@pytest.fixture
def released(db, make_user, publish_item, gateway):
    owner, bidder = make_user("Ana"), make_user("Beto")
    item = publish_item(owner, title="Bicicleta rodado 29")
    o = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000").offer
    tx = offers.accept_offer(db, owner, o.public_id).transaction
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    payments.process_provider_notification(db, pp.provider_payment_id, gateway)
    db.commit()
    return owner, bidder, item, o, tx, pp


def _actions(db):
    db.expire_all()
    return db.execute(select(AuditLog.action)).scalars().all()


def test_item_detail_and_moderation_edit(client, h, make_user, publish_item, db):
    owner, reporter = make_user(), make_user()
    item = publish_item(owner, title="Mesa de roble", description="Mesa linda")
    db.add(Report(reporter_id=reporter.id, item_id=item.id, reason=ReportReason.WRONG_INFO))
    db.commit()
    viewer, mod = h("viewer"), h("mod")
    d = client.get(f"/admin/items/{item.public_id}", headers=viewer).json()
    assert d["item_id"] == item.public_id and d["owner"]["id"] == owner.id and d["zone"] == "Palermo"
    assert d["reports"][0]["reason"] == "WRONG_INFO" and d["channel_status"] == "NOT_POSTED" and d["images"] == 1
    assert owner.test_wa_id not in str(d)  # only the masked number

    body = {"title": "Mesa de roble maciza", "reason": "corrección de título"}
    assert client.patch(f"/admin/items/{item.public_id}", json=body, headers=viewer).status_code == 403
    r = client.patch(f"/admin/items/{item.public_id}", json=body, headers=mod)
    assert r.status_code == 200 and r.json()["title"] == "Mesa de roble maciza"
    bad = {"description": "llamame al 11 5555 4444", "reason": "x" * 5}
    r = client.patch(f"/admin/items/{item.public_id}", json=bad, headers=mod)
    assert r.status_code == 422 and r.json()["error"] == "CONTACT_DATA_IN_TEXT"
    assert client.patch(f"/admin/items/{item.public_id}", json={"reason": "nada"}, headers=mod).status_code == 422
    assert "ADMIN_EDITED_ITEM" in _actions(db)
    # The edit is searchable right away.
    from app.services.search import service as search

    page = search.start_search(db, make_user(), "maciza")
    assert [r["item_id"] for r in page.results] == [item.public_id]


def test_operations_payments_and_offers_lists_carry_codes(client, h, released):
    owner, bidder, item, offer, tx, pp = released
    viewer = h("viewer")
    [t] = client.get("/admin/transactions", headers=viewer).json()
    assert t["transaction_id"] == tx.public_id and t["item_code"] == item.public_id and t["offer_id"] == offer.public_id
    assert t["contact_released"] and t["keyword_sent"]
    d = client.get(f"/admin/transactions/{tx.public_id}", headers=viewer).json()
    assert d["status"] == "CONTACT_RELEASED" and d["payments"][0]["provider_payment_id"] == pp.provider_payment_id
    assert {n["kind"] for n in d["release_notifications"]} == {"CONTACT_RELEASED_OWNER", "CONTACT_RELEASED_BIDDER"}
    assert d["owner"]["id"] == owner.id and d["bidder"]["id"] == bidder.id
    text = str(d)
    assert owner.test_wa_id not in text and bidder.test_wa_id not in text
    with dbmod.session_scope() as s:
        from app.models.offer import Transaction

        kw = s.execute(select(Transaction.security_keyword)).scalar_one()
    assert kw and kw not in text  # the security keyword is never exposed to admins
    [p] = client.get("/admin/payments?status=APPROVED", headers=viewer).json()
    assert p["transaction_code"] == tx.public_id and p["amount"] == "3000.00" and p["currency"] == "ARS"
    [o] = client.get("/admin/offers?status=CONTACT_RELEASED", headers=viewer).json()
    assert o["item_code"] == item.public_id and o["item_title"] == "Bicicleta rodado 29"
    assert client.get("/admin/transactions/TX-NOPE", headers=viewer).status_code == 404


def test_user_profile_includes_offers_reports_and_warnings(client, h, released, db, make_user):

    owner, bidder, item, offer, tx, pp = released
    db.add(Report(reporter_id=owner.id, reported_user_id=bidder.id, reason=ReportReason.SPAM))
    db.commit()
    d = client.get(f"/admin/users/{bidder.id}", headers=h("viewer")).json()
    assert [o["offer_id"] for o in d["offers"]] == [offer.public_id]
    assert d["reports"][0]["reason"] == "SPAM"
    assert client.get("/admin/security/warnings", headers=h("viewer")).json() == []


def test_conversation_browser_is_moderator_only_and_audited_on_open(client, h, make_user, db):
    u = make_user()
    handle_inbound(
        db, wa_id=u.test_wa_id, text="hola", client=MockDolphinClient([say("¡Hola!")]), provider_message_id="wamid.c1"
    )
    handle_inbound(
        db,
        wa_id=u.test_wa_id,
        text="pasame tu numero 1155554444",
        client=MockDolphinClient([]),
        provider_message_id="wamid.c2",
    )
    db.commit()
    assert client.get("/admin/conversations", headers=h("viewer")).status_code == 403
    mod = h("mod")
    [c] = client.get("/admin/conversations", headers=mod).json()
    assert c["id"] == u.id and c["messages"] >= 2 and c["blocked_messages"] == 1 and c["state"]
    assert client.get(f"/admin/conversations?q={u.test_wa_id}", headers=mod).json()[0]["id"] == u.id
    assert client.get("/admin/conversations?q=5491100000999", headers=mod).json() == []
    assert "ADMIN_VIEWED_CONVERSATION" not in _actions(db)  # listing metadata is not reading
    ctx = client.get(f"/admin/conversations/{u.id}/context", headers=mod).json()
    assert ctx["user_id"] == u.id and "state" in ctx
    conv = client.get(f"/admin/conversations/{u.id}", headers=mod).json()
    assert any(m["blocked"] and m["evidence"] for m in conv["messages"])
    assert "ADMIN_VIEWED_CONVERSATION" in _actions(db)
    warnings = client.get(f"/admin/security/warnings?user_id={u.id}", headers=mod).json()
    assert len(warnings) == 1 and warnings[0]["user_id"] == u.id


def test_audit_log_is_admin_only(client, h, make_user, db):
    root = h("root")
    assert client.get("/admin/audit", headers=h("mod")).status_code == 403
    rows = client.get("/admin/audit?action=admin_login", headers=root).json()
    assert rows and all(r["action"] == "ADMIN_LOGIN" for r in rows)
    assert all("password" not in str(r["details"]).lower() for r in rows)
