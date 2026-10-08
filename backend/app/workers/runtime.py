"""Recoverable job runtime (sections 23, 31, 32, 47).

* Every job has a policy of system modes in which it may run (``Job.modes``, section 46).
* Transactional jobs run in one transaction protected by a PostgreSQL advisory lock (several worker replicas
  never run the same job concurrently; a crash rolls the job's work back atomically).
* Before a job runs, a RUNNING marker is committed in ``job_state`` (separate transaction). If the worker dies,
  the marker stays RUNNING; at the next start ``recover_on_startup`` marks it ABANDONED (when the owning worker's
  heartbeat is stale), recovers in-flight outbox sends and re-runs every job once: recover -> reconcile -> continue.
  Jobs derive their work from PostgreSQL state, so re-running them is always safe (idempotent).
* Results go to ``job_state`` (one row per job) and ``job_runs`` (history of runs that did something, failed or
  were abandoned). Errors are stored as exception class + message (no payloads, no secrets).
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.db import session_scope
from app.models.enums import ActorType, JobRunStatus, SystemMode
from app.models.worker import JobRun, JobState, WorkerHeartbeat
from app.services.audit.service import AuditAction, audit
from app.services.system import config_service as cfg

log = logging.getLogger("permutados.worker")

ALL_MODES = frozenset(SystemMode)
HEARTBEAT_SECONDS = 15
STALE_AFTER = timedelta(seconds=HEARTBEAT_SECONDS * 4)


@dataclass(frozen=True)
class Job:
    name: str
    fn: Callable[..., Any]
    interval: int
    modes: frozenset[SystemMode] = ALL_MODES
    transactional: bool = True  # fn(session) inside one locked transaction; else fn() manages its own
    record_idle: bool = True  # update job_state even when nothing happened (False for 2s polling jobs)
    description: str = ""


@dataclass
class RunOutcome:
    status: JobRunStatus
    affected: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def new_worker_id() -> str:
    return f"{socket.gethostname()[:40]}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _affected(value: Any) -> tuple[int, dict[str, Any]]:
    if isinstance(value, dict):
        n = value.get("affected")
        if n is None:
            n = sum(v for v in value.values() if isinstance(v, int) and not isinstance(v, bool))
        return int(n), value
    return int(value or 0), {}


def _lock_key(name: str) -> int:
    return zlib.crc32(f"permutados:{name}".encode())


def _upsert_state(session: Session, name: str, **values: Any) -> None:
    stmt = insert(JobState).values(job_name=name, **values)
    session.execute(stmt.on_conflict_do_update(index_elements=["job_name"], set_=values))


def _mark_running(name: str, worker_id: str, now) -> None:
    with session_scope() as s:
        _upsert_state(s, name, status=JobRunStatus.RUNNING, worker_id=worker_id, last_started_at=now)


def _finish(job: Job, worker_id: str, started, outcome: RunOutcome) -> None:
    now = utcnow()
    ms = int((now - started).total_seconds() * 1000)
    with session_scope() as s:
        values: dict[str, Any] = {
            "status": outcome.status,
            "worker_id": worker_id,
            "last_finished_at": now,
            "last_duration_ms": ms,
            "last_affected": outcome.affected,
            "current_run_id": None,
        }
        st = s.get(JobState, job.name)
        runs = (st.runs if st else 0) + 1
        failures = (st.failures if st else 0) + (1 if outcome.status == JobRunStatus.FAILED else 0)
        values["runs"] = runs
        values["failures"] = failures
        if outcome.status == JobRunStatus.FAILED:
            values["last_error"] = (outcome.error or "")[:500]
            values["consecutive_failures"] = (st.consecutive_failures if st else 0) + 1
        else:
            values["consecutive_failures"] = 0
            values["last_success_at"] = now
            if outcome.result:
                values["last_result"] = outcome.result
        _upsert_state(s, job.name, **values)
        if outcome.status == JobRunStatus.FAILED or outcome.affected:
            s.add(
                JobRun(
                    job_name=job.name,
                    worker_id=worker_id,
                    status=outcome.status,
                    started_at=started,
                    finished_at=now,
                    duration_ms=ms,
                    affected=outcome.affected,
                    result=outcome.result or None,
                    error=outcome.error,
                )
            )


def run_job(job: Job, worker_id: str = "inline") -> RunOutcome:
    """Run one job honouring system mode, advisory lock and bookkeeping. Never raises."""
    started = utcnow()
    try:
        with session_scope() as s:
            mode = cfg.current_mode(s)
        if mode not in job.modes:
            if job.record_idle:
                with session_scope() as s:
                    _upsert_state(s, job.name, status=JobRunStatus.SKIPPED, last_error=f"mode {mode.value}")
            return RunOutcome(JobRunStatus.SKIPPED, result={"reason": f"mode {mode.value}"})

        if job.transactional:
            with session_scope() as s:
                if not s.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _lock_key(job.name)}).scalar():
                    return RunOutcome(JobRunStatus.SKIPPED, result={"reason": "locked by another worker"})
                if job.record_idle:
                    _mark_running(job.name, worker_id, started)
                n, result = _affected(job.fn(s))
                if n:
                    audit(
                        s,
                        AuditAction.WORKER_JOB,
                        actor_type=ActorType.WORKER,
                        details={"job": job.name, "affected": n},
                    )
        else:
            if job.record_idle:
                _mark_running(job.name, worker_id, started)
            n, result = _affected(job.fn())
        outcome = RunOutcome(JobRunStatus.SUCCEEDED, n, result)
    except Exception as e:
        log.exception("job %s failed", job.name)
        outcome = RunOutcome(JobRunStatus.FAILED, error=f"{type(e).__name__}: {str(e)[:300]}")
    if job.record_idle or outcome.affected or outcome.status == JobRunStatus.FAILED:
        try:
            _finish(job, worker_id, started, outcome)
        except Exception:  # bookkeeping must never take the worker down
            log.exception("could not record the outcome of job %s", job.name)
    return outcome


# ---- heartbeats ----------------------------------------------------------------------------------------
def heartbeat(worker_id: str, *, started_at=None, info: dict[str, Any] | None = None) -> None:
    now = utcnow()
    with session_scope() as s:
        stmt = insert(WorkerHeartbeat).values(
            worker_id=worker_id,
            hostname=socket.gethostname()[:120],
            pid=os.getpid(),
            started_at=started_at or now,
            last_seen_at=now,
            info=info or {},
        )
        s.execute(
            stmt.on_conflict_do_update(
                index_elements=["worker_id"], set_={"last_seen_at": now, "info": info or {}, "stopped_at": None}
            )
        )


def mark_stopped(worker_id: str) -> None:
    with session_scope() as s:
        s.execute(update(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker_id).values(stopped_at=utcnow()))


def alive_workers(session: Session, now=None) -> list[WorkerHeartbeat]:
    now = now or utcnow()
    return list(
        session.execute(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.stopped_at.is_(None), WorkerHeartbeat.last_seen_at > now - STALE_AFTER
            )
        ).scalars()
    )


# ---- recovery ---------------------------------------------------------------------------------------------
def recover_on_startup(worker_id: str, *, now=None) -> dict[str, Any]:
    """Recover what a crashed worker left behind. Safe to call while other replicas are alive."""
    from app.services.events.bus import get_bus
    from app.services.notifications.outbox import recover_stale_sending

    now = now or utcnow()
    summary: dict[str, Any] = {}
    with session_scope() as s:
        alive = {w.worker_id for w in alive_workers(s, now)} - {worker_id}
        stuck = (
            s.execute(select(JobState).where(JobState.status == JobRunStatus.RUNNING).with_for_update(skip_locked=True))
            .scalars()
            .all()
        )
        abandoned = []
        for st in stuck:
            if st.worker_id in alive:
                continue  # another live replica is running it right now
            abandoned.append(st.job_name)
            s.add(
                JobRun(
                    job_name=st.job_name,
                    worker_id=st.worker_id,
                    status=JobRunStatus.ABANDONED,
                    started_at=st.last_started_at or now,
                    finished_at=now,
                    error="worker stopped while the job was running; re-run at start-up",
                )
            )
            st.status = JobRunStatus.ABANDONED
            st.last_error = "abandoned (worker crash)"
        summary["abandoned_jobs"] = abandoned
        # Heartbeats of dead workers.
        s.execute(
            update(WorkerHeartbeat)
            .where(WorkerHeartbeat.stopped_at.is_(None), WorkerHeartbeat.last_seen_at <= now - STALE_AFTER)
            .values(stopped_at=now)
        )
    summary["outbox_in_flight_recovered"] = recover_stale_sending(now=now)
    with session_scope() as s:
        summary["event_handlers_recovered"] = get_bus().retry_failed(s, now=now + timedelta(days=1))
        if any(v for v in summary.values()):
            audit(s, AuditAction.WORKER_RECOVERY, actor_type=ActorType.WORKER, details={"worker": worker_id, **summary})
    log.info("startup recovery: %s", summary)
    return summary


def prune_history(session: Session, now=None) -> dict[str, int]:
    """Retention for bookkeeping tables (audit logs are append-only and never pruned)."""
    from app.models.item import ItemInteraction

    now = now or utcnow()
    r1 = session.execute(delete(JobRun).where(JobRun.started_at < now - timedelta(days=30))).rowcount or 0
    r2 = (
        session.execute(
            delete(WorkerHeartbeat).where(
                WorkerHeartbeat.stopped_at.is_not(None), WorkerHeartbeat.stopped_at < now - timedelta(days=7)
            )
        ).rowcount
        or 0
    )
    r3 = (
        session.execute(delete(ItemInteraction).where(ItemInteraction.day < (now - timedelta(days=60)).date())).rowcount
        or 0
    )
    return {"job_runs": r1, "heartbeats": r2, "interactions": r3}


def run_forever_once(jobs: list[Job], worker_id: str) -> dict[str, RunOutcome]:
    """Run every job once (start-up recovery pass / tests)."""
    out = {}
    for j in jobs:
        t0 = time.monotonic()
        out[j.name] = run_job(j, worker_id)
        log.debug("job %s took %.1fms", j.name, (time.monotonic() - t0) * 1000)
    return out
