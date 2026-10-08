"""Recoverable job runtime and the real worker process (real PostgreSQL, real subprocess)."""

import os
import signal
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text, update

from app.core import db as dbmod
from app.core.clock import utcnow
from app.models.audit import AuditLog
from app.models.channel import ChannelPost
from app.models.conversation import Notification
from app.models.enums import (
    AdminRole,
    ChannelPostStatus,
    InteractionKind,
    ItemStatus,
    JobRunStatus,
    NotificationStatus,
    OfferStatus,
    SystemMode,
)
from app.models.item import Item, ItemInteraction
from app.models.offer import Offer
from app.models.system import SystemConfig
from app.models.worker import JobRun, JobState, WorkerHeartbeat
from app.services.admin.auth import create_admin
from app.services.channel import service as channel
from app.services.notifications.service import enqueue
from app.services.offers import service as offers
from app.services.system import config_service as cfg
from app.workers import runtime
from app.workers.jobs import maintenance_jobs
from app.workers.runtime import Job, recover_on_startup, run_job

BACKEND = Path(__file__).resolve().parents[2]


def _jobs():
    return {j.name: j for j in maintenance_jobs()}


def _state(name):
    with dbmod.session_scope() as s:
        st = s.get(JobState, name)
        if st is None:
            return None
        s.expunge(st)
        return st


def _runs(name):
    with dbmod.session_scope() as s:
        return [
            (r.status, r.affected, r.error) for r in s.execute(select(JobRun).where(JobRun.job_name == name)).scalars()
        ]


def _expired_offer(db, make_user, publish_item):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    res = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="1")
    db.execute(text("UPDATE offers SET created_at = now() - interval '2 days', expires_at = now() - interval '1 day'"))
    db.commit()
    return item, res.offer


def test_mode_policy_skips_deadline_jobs_when_paused(db, make_user, publish_item):
    item, offer = _expired_offer(db, make_user, publish_item)
    cfg.set_value(db, "system_mode", SystemMode.PAUSED.value)
    db.commit()
    jobs = _jobs()
    out = run_job(jobs["expire_pending_offers"], "w1")
    assert out.status == JobRunStatus.SKIPPED and out.result["reason"] == "mode PAUSED"
    assert _state("expire_pending_offers").status == JobRunStatus.SKIPPED
    db.expire_all()
    assert db.get(Offer, offer.id).status == OfferStatus.PENDING  # frozen while paused
    # Safe jobs keep running while paused.
    assert run_job(jobs["expire_publications"], "w1").status == JobRunStatus.SUCCEEDED
    assert run_job(jobs["channel_reconciliation"], "w1").status == JobRunStatus.SUCCEEDED
    cfg.set_value(db, "system_mode", SystemMode.EMERGENCY.value)
    db.commit()
    assert run_job(jobs["refresh_channel_scores"], "w1").status == JobRunStatus.SKIPPED
    assert run_job(jobs["release_deferred"], "w1").status == JobRunStatus.SKIPPED
    assert run_job(jobs["retry_event_handlers"], "w1").status == JobRunStatus.SUCCEEDED
    cfg.set_value(db, "system_mode", SystemMode.ONLINE.value)
    db.commit()
    out = run_job(jobs["expire_pending_offers"], "w1")
    assert out.status == JobRunStatus.SUCCEEDED and out.affected == 1
    db.expire_all()
    assert db.get(Offer, offer.id).status == OfferStatus.EXPIRED
    assert db.get(Item, item.id).status == ItemStatus.PUBLISHED  # item goes back to the catalogue
    st = _state("expire_pending_offers")
    assert st.status == JobRunStatus.SUCCEEDED and st.last_affected == 1 and st.consecutive_failures == 0
    assert _runs("expire_pending_offers") == [(JobRunStatus.SUCCEEDED, 1, None)]
    # An idle re-run (recovery after restart) is a no-op and does not grow the history.
    assert run_job(jobs["expire_pending_offers"], "w1").affected == 0
    assert len(_runs("expire_pending_offers")) == 1 and _state("expire_pending_offers").runs == 2


def test_advisory_lock_prevents_concurrent_runs(db):
    job = _jobs()["expire_publications"]
    with dbmod.get_engine().connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": runtime._lock_key(job.name)})
        out = run_job(job, "w2")
        assert out.status == JobRunStatus.SKIPPED and "locked" in out.result["reason"]
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": runtime._lock_key(job.name)})
    assert run_job(job, "w2").status == JobRunStatus.SUCCEEDED


def test_failing_job_rolls_back_and_is_recorded(db):
    calls = {"n": 0}

    def flaky(session):
        calls["n"] += 1
        session.execute(update(SystemConfig).where(SystemConfig.key == "channel_daily_limit").values(value=99))
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")
        return 0

    job = Job("flaky", flaky, 60)
    out = run_job(job, "w3")
    assert out.status == JobRunStatus.FAILED and "database hiccup" in out.error
    db.expire_all()
    assert cfg.get_value(db, "channel_daily_limit") != 99  # the failed run's writes were rolled back
    st = _state("flaky")
    assert st.status == JobRunStatus.FAILED and st.consecutive_failures == 1 and st.failures == 1
    assert _runs("flaky")[0][0] == JobRunStatus.FAILED
    assert run_job(job, "w3").status == JobRunStatus.SUCCEEDED
    st = _state("flaky")
    assert st.consecutive_failures == 0 and st.failures == 1 and st.runs == 2 and st.last_success_at


def test_non_transactional_job_and_dict_results():
    out = run_job(Job("nt", lambda: {"sent": 2, "failed": 1}, 5, transactional=False), "w4")
    assert out.affected == 3 and out.result == {"sent": 2, "failed": 1}
    quiet = Job("poll", lambda: 0, 2, transactional=False, record_idle=False)
    run_job(quiet, "w4")
    assert _state("poll") is None  # 2-second pollers do not write bookkeeping when idle


def test_startup_recovery_abandoned_jobs_heartbeats_and_outbox(db, make_user):
    now = utcnow()
    with dbmod.session_scope() as s:
        s.add_all(
            [
                WorkerHeartbeat(
                    worker_id="dead", hostname="h", pid=1, started_at=now, last_seen_at=now - timedelta(minutes=5)
                ),
                WorkerHeartbeat(worker_id="live", hostname="h", pid=2, started_at=now, last_seen_at=now),
                JobState(job_name="job_dead", status=JobRunStatus.RUNNING, worker_id="dead", last_started_at=now),
                JobState(job_name="job_live", status=JobRunStatus.RUNNING, worker_id="live", last_started_at=now),
            ]
        )
    u = make_user()
    n = enqueue(db, user_id=u.id, kind="REPLY", body="x", idempotency_key="inflight")
    db.flush()
    n.status = NotificationStatus.SENDING
    n.sending_started_at = now - timedelta(hours=1)
    n.payload = {"sending": {"mode": "SESSION"}}
    db.commit()

    summary = recover_on_startup("me")
    assert summary["abandoned_jobs"] == ["job_dead"] and summary["outbox_in_flight_recovered"] == 1
    assert _state("job_dead").status == JobRunStatus.ABANDONED
    assert _state("job_live").status == JobRunStatus.RUNNING  # a live replica is still running it
    assert _runs("job_dead")[0][0] == JobRunStatus.ABANDONED
    db.expire_all()
    assert db.get(WorkerHeartbeat, "dead").stopped_at is not None
    assert db.get(WorkerHeartbeat, "live").stopped_at is None
    assert db.get(Notification, n.id).status == NotificationStatus.PENDING
    assert db.execute(select(AuditLog).where(AuditLog.action == "WORKER_RECOVERY")).scalars().first()
    assert {w.worker_id for w in runtime.alive_workers(db)} == {"live"}
    # Second start-up: nothing left to recover.
    assert recover_on_startup("me2")["abandoned_jobs"] == []


def test_prune_history(db, make_user, publish_item):
    now = utcnow()
    owner, other = make_user(), make_user()
    item = publish_item(owner)
    with dbmod.session_scope() as s:
        s.add_all(
            [
                JobRun(job_name="j", status=JobRunStatus.SUCCEEDED, started_at=now - timedelta(days=40)),
                JobRun(job_name="j", status=JobRunStatus.SUCCEEDED, started_at=now - timedelta(days=1)),
                WorkerHeartbeat(
                    worker_id="old",
                    hostname="h",
                    pid=1,
                    started_at=now - timedelta(days=20),
                    last_seen_at=now - timedelta(days=10),
                    stopped_at=now - timedelta(days=10),
                ),
                ItemInteraction(
                    item_id=item.id, user_id=other.id, kind=InteractionKind.VIEW, day=(now - timedelta(days=70)).date()
                ),
                ItemInteraction(item_id=item.id, user_id=other.id, kind=InteractionKind.VIEW, day=now.date()),
            ]
        )
    with dbmod.session_scope() as s:
        assert runtime.prune_history(s) == {"job_runs": 1, "heartbeats": 1, "interactions": 1}


# ---- the real worker process -----------------------------------------------------------------------------
def _worker_env():
    env = dict(os.environ)
    # No external services from the subprocess: no WhatsApp sends, no Mercado Pago calls, no Dolphin.
    env.update(WHATSAPP_ACCESS_TOKEN="", MERCADOPAGO_ACCESS_TOKEN="", DOLPHIN_BASE_URL="", LOG_LEVEL="INFO")
    env["PYTHONPATH"] = str(BACKEND)
    return env


def _start_worker():
    return subprocess.Popen(
        [sys.executable, "-m", "app.workers.scheduler"],
        cwd=BACKEND,
        env=_worker_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait(pred, proc, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        if proc.poll() is not None:
            pytest.fail("worker exited early:\n" + (proc.stdout.read() if proc.stdout else ""))
        time.sleep(0.3)
    return False


def _heartbeats():
    with dbmod.session_scope() as s:
        return {w.worker_id: (w.last_seen_at, w.stopped_at) for w in s.execute(select(WorkerHeartbeat)).scalars()}


def test_worker_process_recovers_reconciles_and_stops_cleanly(db, make_user, publish_item):
    admin = create_admin(db, "root", "root-password-123456", AdminRole.ADMIN)
    owner = make_user()
    item = publish_item(owner)
    post = channel.register_manual_post(db, admin.id, item.public_id, external_ref="https://wa.me/c/1")
    db.commit()
    # State left behind by a crashed worker during "downtime".
    db.execute(update(Item).where(Item.id == item.id).values(expires_at=utcnow() - timedelta(hours=1)))
    now = utcnow()
    db.add(
        WorkerHeartbeat(
            worker_id="crashed",
            hostname="h",
            pid=1,
            started_at=now - timedelta(hours=2),
            last_seen_at=now - timedelta(minutes=10),
        )
    )
    db.add(
        JobState(
            job_name="expire_publications",
            status=JobRunStatus.RUNNING,
            worker_id="crashed",
            last_started_at=now - timedelta(minutes=10),
        )
    )
    db.commit()

    proc = _start_worker()
    try:

        def recovered():
            st = _state("expire_publications")
            return st is not None and st.status == JobRunStatus.SUCCEEDED and st.last_affected == 1

        assert _wait(recovered, proc), "start-up pass did not run"
        db.expire_all()
        assert db.get(Item, item.id).status == ItemStatus.EXPIRED  # expired during downtime -> caught up
        assert db.get(ChannelPost, post.id).status == ChannelPostStatus.REMOVAL_PENDING  # event-driven flag
        assert (JobRunStatus.ABANDONED, None) in [(r[0], r[1]) for r in _runs("expire_publications")]
        hb = _heartbeats()
        assert hb["crashed"][1] is not None  # dead worker marked stopped
        mine = [k for k, v in hb.items() if k != "crashed"]
        assert len(mine) == 1 and hb[mine[0]][1] is None
        assert db.execute(select(AuditLog).where(AuditLog.action == "WORKER_RECOVERY")).scalars().first()
        # Crash (SIGKILL): the heartbeat stays "open" — the next start-up will mark it once stale.
        proc.kill()
        proc.wait(10)
        assert _heartbeats()[mine[0]][1] is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)

    # Restart: then a graceful stop (SIGTERM) marks the heartbeat stopped.
    with dbmod.session_scope() as s:
        s.execute(
            update(WorkerHeartbeat)
            .where(WorkerHeartbeat.worker_id == mine[0])
            .values(last_seen_at=utcnow() - timedelta(minutes=5))
        )
    proc = _start_worker()
    try:
        assert _wait(lambda: len(_heartbeats()) == 3, proc)
        assert _wait(lambda: _heartbeats()[mine[0]][1] is not None, proc)  # the killed one, recovered
        time.sleep(1.0)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(20) == 0
        new = [k for k in _heartbeats() if k not in ("crashed", mine[0])][0]
        assert _heartbeats()[new][1] is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
