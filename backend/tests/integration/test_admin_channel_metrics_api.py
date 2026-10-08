"""Admin HTTP API for the manual channel, metrics and worker status (real app, real PostgreSQL)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core import db as dbmod
from app.core.clock import utcnow
from app.models.audit import AuditLog
from app.models.enums import AdminRole
from app.services.admin.auth import create_admin
from app.services.offers import service as offers
from app.services.payments import service as payments
from app.services.search import service as search
from app.services.system import config_service as cfg
from app.workers import runtime

PASSWORDS = {"root": "root-password-123456", "viewer": "viewer-password-123", "mod": "moderator-pass-1234"}


@pytest.fixture
def h(client):
    with dbmod.session_scope() as s:
        create_admin(s, "root", PASSWORDS["root"], AdminRole.ADMIN)
        create_admin(s, "viewer", PASSWORDS["viewer"], AdminRole.VIEWER)
        create_admin(s, "mod", PASSWORDS["mod"], AdminRole.MODERATOR)
        cfg.set_value(s, "channel_enabled", True)
        cfg.set_value(s, "channel_min_score", 0)

    def _h(user):
        r = client.post("/admin/auth/login", json={"username": user, "password": PASSWORDS[user]})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    return _h


def test_manual_channel_workflow_over_http(client, h, make_user, publish_item, db):
    viewer, mod = h("viewer"), h("mod")
    owner = make_user()
    a = publish_item(owner, title="Mesa", category="muebles")
    b = publish_item(make_user(), title="Bici", category="bicicletas")

    rep = client.get("/admin/channel/suggestions?limit=5", headers=viewer).json()
    assert rep["enabled"] and {i["item_id"] for i in rep["items"]} == {a.public_id, b.public_id}
    assert "score_breakdown" in rep["items"][0] and rep["quota"]["daily_limit"] == 10

    assert client.post("/admin/channel/queue", json={"item_ids": [a.public_id]}, headers=viewer).status_code == 403
    r = client.post("/admin/channel/queue", json={"item_ids": [a.public_id]}, headers=mod)
    assert r.status_code == 201
    post_id = r.json()[0]["id"]
    assert r.json()[0]["channel_status"] == "QUEUED"
    r = client.post(
        f"/admin/channel/posts/{post_id}/published",
        json={"external_ref": "https://whatsapp.com/channel/p/1"},
        headers=mod,
    )
    assert r.status_code == 200 and r.json()["status"] == "PUBLISHED" and r.json()["channel_status"] == "VISIBLE"

    r = client.post("/admin/channel/posts", json={"item_id": b.public_id, "external_ref": None}, headers=mod)
    assert r.status_code == 201
    assert client.post("/admin/channel/posts", json={"item_id": b.public_id}, headers=mod).status_code == 409

    assert client.post(f"/admin/items/{b.public_id}/feature", json={"featured": True}, headers=mod).json()["featured"]
    score = client.get(f"/admin/items/{a.public_id}/score", headers=viewer).json()
    assert score["channel_status"] == "VISIBLE" and score["exposure_penalty"] > 0 and 0 <= score["score"] <= 100

    # The owner deletes the item: the post shows up in the removals list immediately.
    from app.services.publications import service as pubs

    pubs.delete_publication(db, owner, a.id)
    db.commit()
    rem = client.get("/admin/channel/removals", headers=viewer).json()
    assert [r["id"] for r in rem["removal_pending"]] == [post_id]
    assert client.post(f"/admin/channel/posts/{post_id}/removed", headers=mod).json()["status"] == "REMOVED"
    assert client.get("/admin/channel/removals", headers=viewer).json()["removal_pending"] == []

    ov = client.get("/admin/channel/overview", headers=viewer).json()
    assert ov["mode"] == "MANUAL" and ov["posts_by_status"]["REMOVED"] == 1 and ov["posts_by_status"]["PUBLISHED"] == 1
    assert ov["quota"]["posted_last_24h"] == 2
    posts = client.get("/admin/channel/posts?status=PUBLISHED", headers=viewer).json()
    assert [p["item_id"] for p in posts] == [b.public_id]
    rec = client.post("/admin/channel/reconcile", headers=mod).json()
    assert rec["affected"] == 0 and "checked_at" in rec
    assert client.post("/admin/channel/refresh-scores", headers=mod).status_code == 200
    actions = db.execute(select(AuditLog.action)).scalars().all()
    assert {"CHANNEL_POST_CREATED", "CHANNEL_POST_REMOVED", "ADMIN_CHANNEL_ACTION"} <= set(actions)


def test_metrics_reflect_real_activity(client, h, make_user, publish_item, db, gateway):
    owner, bidder, other = make_user(), make_user(), make_user()
    item = publish_item(owner, title="Bicicleta rodado 29")
    item2 = publish_item(other, title="Silla gamer", category="muebles")
    search.start_search(db, bidder, "bicicleta")
    search.start_search(db, bidder, "zzzz inexistente")
    o = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1000").offer
    tx = offers.accept_offer(db, owner, o.public_id).transaction
    link = payments.create_payment(db, bidder, tx.public_id, gateway)
    o2 = offers.create_offer(db, owner, item2.public_id, offer_type="MONEY", offered_price="10").offer
    offers.reject_offer(db, other, o2.public_id)
    db.commit()
    pp = gateway.simulate_payment(link.payment.external_reference)
    payments.process_provider_notification(db, pp.provider_payment_id, gateway)
    db.commit()
    runtime.heartbeat("metrics-worker")

    viewer = h("viewer")
    assert client.get("/admin/metrics?days=0", headers=viewer).status_code == 422
    m = client.get("/admin/metrics?days=7", headers=viewer).json()
    assert m["period_days"] == 7 and m["system_mode"] == "ONLINE"
    assert m["users"]["total"] == 3 and m["users"]["new"] == 3
    assert m["publications"]["by_status"]["COMPLETED"] == 1 and m["publications"]["published_in_period"] == 2
    assert m["searches"]["sessions"] == 2 and m["searches"]["zero_result_sessions"] == 1
    assert m["offers"]["created"] == 2 and m["offers"]["accepted"] == 1 and m["offers"]["rejected"] == 1
    assert m["offers"]["acceptance_rate"] == 0.5
    assert m["payments"]["approved"] == 1
    assert Decimal(m["payments"]["revenue"]["ARS"]) == Decimal("3000")
    assert m["contacts"]["released"] == 1
    assert m["conversion"]["offer_to_release"] == 0.5 and m["conversion"]["acceptance_to_payment"] == 1.0
    assert [w["worker_id"] for w in m["performance"]["workers_alive"]] == ["metrics-worker"]
    assert set(m["performance"]["outbox_delivery"]) == {"p50_seconds", "p95_seconds", "avg_seconds", "samples"}
    today = [d for d in m["daily"] if d["offers"]]
    assert sum(d["offers"] for d in m["daily"]) == 2 and today and Decimal(today[-1]["revenue"]) == Decimal("3000")
    assert len(m["daily"]) == 8
    # No personal data in the dashboard payload.
    body = str(m)
    assert bidder.test_wa_id not in body and owner.test_wa_id not in body


def test_workers_endpoint_and_health(client, h):
    viewer = h("viewer")
    assert client.get("/admin/system/health", headers=viewer).json()["worker"] == "down"
    runtime.heartbeat("w-alive")
    with dbmod.session_scope() as s:
        from app.models.worker import WorkerHeartbeat

        s.add(
            WorkerHeartbeat(
                worker_id="w-old",
                hostname="h",
                pid=1,
                started_at=utcnow() - timedelta(hours=1),
                last_seen_at=utcnow() - timedelta(minutes=10),
            )
        )
    from app.workers.jobs import maintenance_jobs

    runtime.run_job(next(j for j in maintenance_jobs() if j.name == "expire_publications"), "w-alive")
    w = client.get("/admin/workers", headers=viewer).json()
    alive = {x["worker_id"]: x["alive"] for x in w["workers"]}
    assert alive == {"w-alive": True, "w-old": False}
    assert [j["job"] for j in w["jobs"]] == ["expire_publications"] and w["jobs"][0]["status"] == "SUCCEEDED"
    health = client.get("/admin/system/health", headers=viewer).json()
    assert health["worker"] == "ok" and health["workers_alive"] == 1
    assert client.get("/admin/workers").status_code == 401
