"""Field-level encryption of PII: at rest, in lookups, in admin views, in the migration, and in code paths."""

import ast
import pathlib

import pytest
from sqlalchemy import select, text

from app.core import crypto
from app.core import db as dbmod
from app.models.user import User
from app.services.users import pii
from app.services.users.service import find_by_whatsapp, get_or_create_by_whatsapp

APP = pathlib.Path(__file__).resolve().parents[2] / "app"


def dump_all_tables() -> str:
    """Every row of every table as JSON text (what an attacker with a DB dump would see)."""
    with dbmod.get_engine().connect() as c:
        tables = [r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))]
        return "\n".join(
            str(row[0])
            for t in tables
            for row in c.execute(text(f'SELECT row_to_json(x)::text FROM "{t}" x'))  # noqa: S608
        )


def test_user_row_has_no_plaintext_and_lookup_works(db):
    u = get_or_create_by_whatsapp(db, "+54 9 11 6123-4567", "Ana Pérez")
    db.commit()
    dump = dump_all_tables()
    assert "5491161234567" not in dump and "61234567" not in dump and "Ana Pérez" not in dump
    assert u.whatsapp_last4 == "4567" and pii.masked_whatsapp(u) == "•••4567"
    assert find_by_whatsapp(db, "5491161234567").id == u.id
    assert get_or_create_by_whatsapp(db, "5491161234567").id == u.id  # same identity, no duplicate
    assert pii.reveal_whatsapp_for_release(u) == "5491161234567"


def test_wrong_key_cannot_read_pii(db):
    u = get_or_create_by_whatsapp(db, "5491161234567")
    db.commit()
    other = crypto.Cipher(crypto.generate_key())
    with pytest.raises(crypto.CryptoError):
        other.decrypt(u.whatsapp_enc, crypto.WA_ID)


ALLOWED_DECRYPT_MODULES = {"app/core/crypto.py", "app/services/users/pii.py"}
REVEAL_CALLERS = {
    "reveal_whatsapp_for_release": {"app/services/payments/service.py"},
    "reveal_whatsapp_for_delivery": {"app/services/notifications/outbox.py"},
    "reveal_notification_body": {"app/services/notifications/outbox.py"},
    "open_inbound_envelope": {"app/services/conversation/inbound_queue.py"},
    "reveal_display_name_for_admin": {"app/api/routes/admin/routes.py"},
    "reveal_message_evidence_for_admin": {"app/api/routes/admin/routes.py"},
}


def test_architecture_decryption_only_in_named_places():
    decrypt_sites, reveal_sites = set(), {k: set() for k in REVEAL_CALLERS}
    for path in APP.rglob("*.py"):
        rel = str(path.relative_to(APP.parent))
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                if name == "decrypt":
                    decrypt_sites.add(rel)
                if name in reveal_sites:
                    reveal_sites[name].add(rel)
    assert decrypt_sites <= ALLOWED_DECRYPT_MODULES, decrypt_sites
    for fn, callers in reveal_sites.items():
        assert callers <= REVEAL_CALLERS[fn], (fn, callers)
    assert reveal_sites["reveal_whatsapp_for_release"] == {"app/services/payments/service.py"}


def test_admin_views_mask_and_audit(client, db, make_user):
    from app.models.enums import AdminRole
    from app.services.admin.auth import create_admin
    from app.services.conversation.service import handle_inbound
    from app.services.dolphin.mock import MockDolphinClient

    with dbmod.session_scope() as s:
        create_admin(s, "mod", "moderator-pass-1234", AdminRole.MODERATOR)
    u = get_or_create_by_whatsapp(db, "5491177778888", "Beto")
    db.commit()
    handle_inbound(
        db,
        wa_id="5491177778888",
        text="pasame tu cel 11 2222 3333",
        client=MockDolphinClient(),
        provider_message_id="w1",
    )
    db.commit()
    tok = client.post("/admin/auth/login", json={"username": "mod", "password": "moderator-pass-1234"}).json()
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    listing = client.get("/admin/users", headers=h).json()
    assert listing[0]["whatsapp"] == "•••8888" and "5491177778888" not in str(listing)
    assert client.get("/admin/users", params={"q": "+54 9 11 7777-8888"}, headers=h).json()[0]["id"] == u.id
    assert client.get("/admin/users", params={"q": "7777"}, headers=h).json() == []  # no partial search
    detail = client.get(f"/admin/users/{u.id}", headers=h).json()
    assert detail["display_name"] == "Beto"
    conv = client.get(f"/admin/conversations/{u.id}", headers=h).json()
    blocked = [m for m in conv["messages"] if m["blocked"]][0]
    assert blocked["body"] is None and "2222 3333" in blocked["evidence"]


def test_full_flow_leaves_no_plaintext_numbers(db, make_user, publish_item, gateway):
    from app.services.offers import service as offers
    from app.services.payments import service as payments

    owner, bidder = make_user("Olga"), make_user("Bruno")
    item = publish_item(owner)
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000")
    acc = offers.accept_offer(db, owner, res.offer.public_id)
    link = payments.create_payment(db, bidder, acc.transaction.public_id, gateway)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    payments.process_provider_notification(db, pp.provider_payment_id, gateway)
    db.commit()
    dump = dump_all_tables()
    for wa in (owner.test_wa_id, bidder.test_wa_id):
        assert wa not in dump
    assert "Olga" not in dump and "Bruno" not in dump


def test_migration_encrypts_existing_rows_and_downgrade_restores(db):
    """Real data migration: phase-1 plaintext rows -> 0002 encrypted -> downgrade -> plaintext again."""
    from alembic import command

    from tests.conftest import make_alembic_config

    db.close()
    cfg = make_alembic_config()
    command.downgrade(cfg, "0001")
    try:
        with dbmod.get_engine().begin() as c:
            uid = c.execute(
                text(
                    "INSERT INTO users (whatsapp_id, phone, display_name) VALUES ('5491188889999', '5491188889999', "
                    "'Carla') RETURNING id"
                )
            ).scalar_one()
            c.execute(
                text(
                    "INSERT INTO messages (user_id, direction, message_type, body, blocked) "
                    "VALUES (:u, 'INBOUND', 'TEXT', 'mi cel 11 8888 9999', true)"
                ),
                {"u": uid},
            )
            c.execute(
                text(
                    "INSERT INTO notifications (user_id, kind, body, idempotency_key, template_name) VALUES "
                    "(:u, 'CONTACT_RELEASED_OWNER', 'WhatsApp +5491100001111 LUNA-1111', 'k1', "
                    "'contact_released')"
                ),
                {"u": uid},
            )
        command.upgrade(cfg, "head")
        dump = dump_all_tables()
        assert "5491188889999" not in dump and "Carla" not in dump and "8888 9999" not in dump
        assert "5491100001111" not in dump
        with dbmod.session_scope() as s:
            u = s.execute(select(User).where(User.whatsapp_hash == pii.whatsapp_hash("5491188889999"))).scalar_one()
            assert pii.reveal_whatsapp_for_release(u) == "5491188889999" and u.whatsapp_last4 == "9999"
            assert pii.reveal_display_name_for_admin(u) == "Carla"
            row = s.execute(text("SELECT template_name, sensitive FROM notifications")).one()
            assert row == (None, True)
        command.downgrade(cfg, "0001")
        with dbmod.get_engine().connect() as c:
            assert c.execute(text("SELECT whatsapp_id, display_name FROM users")).one() == ("5491188889999", "Carla")
            assert c.execute(text("SELECT body FROM messages")).scalar_one() == "mi cel 11 8888 9999"
            assert "5491100001111" in c.execute(text("SELECT body FROM notifications")).scalar_one()
            c.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))
            c.commit()
    finally:
        command.upgrade(cfg, "head")


def test_key_rotation_reencrypts_and_reindexes(db, monkeypatch):
    from app.services.notifications.service import enqueue

    old_key = crypto.generate_key()
    old = crypto.Cipher(old_key)
    monkeypatch.setattr(crypto, "get_cipher", lambda: old)
    u = get_or_create_by_whatsapp(db, "5491155556666", "Dora")
    enqueue(
        db, user_id=u.id, kind="CONTACT_RELEASED_OWNER", body="+5491100002222", idempotency_key="r1", sensitive=True
    )
    db.commit()
    new = crypto.Cipher(crypto.generate_key(), old=old_key)
    counts = pii.rotate_encryption(db, new)
    db.commit()
    assert counts == {"users": 1, "messages": 0, "notifications": 1}
    only_new = crypto.Cipher(crypto.generate_key())  # sanity: unrelated key still fails
    monkeypatch.setattr(crypto, "get_cipher", lambda: new)
    db.expire_all()
    u = find_by_whatsapp(db, "5491155556666")
    assert u is not None and not new.needs_rotation(u.whatsapp_enc)
    assert pii.reveal_whatsapp_for_release(u) == "5491155556666" and pii.reveal_display_name_for_admin(u) == "Dora"
    with pytest.raises(crypto.CryptoError):
        old.decrypt(u.whatsapp_enc, crypto.WA_ID)
    with pytest.raises(crypto.CryptoError):
        only_new.decrypt(u.whatsapp_enc, crypto.WA_ID)
