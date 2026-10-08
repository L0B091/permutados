import pytest
from sqlalchemy import select

from app.core import db as dbmod
from app.models.audit import AuditLog
from app.models.enums import AdminRole
from app.services.admin.auth import create_admin

PASSWORDS = {"root": "root-password-123456", "viewer": "viewer-password-123", "mod": "moderator-pass-1234"}


@pytest.fixture
def admins():
    with dbmod.session_scope() as s:
        create_admin(s, "root", PASSWORDS["root"], AdminRole.ADMIN)
        create_admin(s, "viewer", PASSWORDS["viewer"], AdminRole.VIEWER)
        create_admin(s, "mod", PASSWORDS["mod"], AdminRole.MODERATOR)


def login(client, user):
    r = client.post("/admin/auth/login", json={"username": user, "password": PASSWORDS[user]})
    assert r.status_code == 200, r.text
    return r.json()


def auth(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def actions():
    with dbmod.session_scope() as s:
        return s.execute(select(AuditLog.action)).scalars().all()


def test_login_and_me(client, admins):
    t = login(client, "root")
    assert t["role"] == "ADMIN" and t["token_type"] == "bearer"
    assert client.get("/admin/auth/me", headers=auth(t)).json()["username"] == "root"
    assert "ADMIN_LOGIN" in actions()


def test_unauthenticated_and_tampered_tokens_rejected(client, admins):
    assert client.get("/admin/dashboard").status_code == 401
    t = login(client, "root")
    bad = t["access_token"][:-3] + "abc"
    assert client.get("/admin/dashboard", headers={"Authorization": f"Bearer {bad}"}).status_code == 401
    assert client.get("/admin/dashboard", headers={"Authorization": f"Bearer {t['refresh_token']}"}).status_code == 401


def test_wrong_password_and_lockout(client, admins):
    for _ in range(5):
        assert (
            client.post("/admin/auth/login", json={"username": "root", "password": "nope-nope-nope"}).status_code == 401
        )
    r = client.post("/admin/auth/login", json={"username": "root", "password": PASSWORDS["root"]})
    assert r.status_code == 401 and r.json()["error"] == "ACCOUNT_LOCKED"
    assert actions().count("ADMIN_LOGIN_FAILED") >= 6


def test_login_rate_limited(client, admins):
    codes = [
        client.post("/admin/auth/login", json={"username": f"x{i}", "password": "whatever-123"}).status_code
        for i in range(12)
    ]
    assert 429 in codes


def test_rbac(client, admins):
    viewer, mod, root = login(client, "viewer"), login(client, "mod"), login(client, "root")
    assert client.get("/admin/dashboard", headers=auth(viewer)).status_code == 200
    assert client.put("/admin/config", json={"contact_release_price": 1}, headers=auth(viewer)).status_code == 403
    assert client.put("/admin/config", json={"contact_release_price": 1}, headers=auth(mod)).status_code == 403
    assert (
        client.put("/admin/system/mode", json={"mode": "PAUSED", "reason": "test"}, headers=auth(mod)).status_code
        == 403
    )
    r = client.put("/admin/config", json={"contact_release_price": 3500}, headers=auth(root))
    assert r.status_code == 200 and r.json()["contact_release_price"] == "3500.00"
    assert client.put("/admin/config", json={"contact_release_currency": "USD"}, headers=auth(root)).status_code == 422
    assert client.put(
        "/admin/system/mode", json={"mode": "PAUSED", "reason": "mantenimiento"}, headers=auth(root)
    ).json() == {"mode": "PAUSED"}
    a = actions()
    assert "ADMIN_CHANGED_CONFIG" in a and "ADMIN_CHANGED_SYSTEM_MODE" in a


def test_refresh_rotation_and_reuse_detection(client, admins):
    t1 = login(client, "root")
    r = client.post("/admin/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r.status_code == 200
    t2 = r.json()
    assert t2["refresh_token"] != t1["refresh_token"]
    # reusing the rotated token revokes the whole session family
    r = client.post("/admin/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert r.status_code == 401 and r.json()["error"] == "REFRESH_TOKEN_REUSED"
    assert client.post("/admin/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401
    assert client.get("/admin/auth/me", headers=auth(t2)).status_code == 401


def test_logout_invalidates_access_token(client, admins):
    t = login(client, "root")
    assert client.post("/admin/auth/logout", headers=auth(t)).status_code == 204
    assert client.get("/admin/auth/me", headers=auth(t)).status_code == 401


def test_admin_views_are_audited(client, admins, db, make_user):
    u = make_user()
    t = login(client, "mod")
    assert client.get(f"/admin/users/{u.id}", headers=auth(t)).status_code == 200
    assert client.get(f"/admin/conversations/{u.id}", headers=auth(t)).status_code == 200
    a = actions()
    assert "ADMIN_VIEWED_USER" in a and "ADMIN_VIEWED_CONVERSATION" in a


def test_security_headers_and_cors(client):
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in r.headers
