"""Worker process: ``python -m app.workers.scheduler``.

Start-up sequence (section 32/47: RECOVER JOBS -> RECONCILE -> CONTINUE):
1. register a heartbeat;
2. ``recover_on_startup``: abandoned job runs, notifications left in SENDING, failed event handlers;
3. run every maintenance job once (expiries, payment reconciliation, channel reconciliation, ...), honouring
   the system mode;
4. schedule everything on its interval. Jobs are idempotent, mode-aware and serialised with advisory locks, so
   several replicas or a restart in the middle of a run are safe.
SIGTERM/SIGINT stop the scheduler cleanly and mark the heartbeat as stopped.
"""

from __future__ import annotations

import logging
import signal

from apscheduler.schedulers.blocking import BlockingScheduler

from app.core.clock import utcnow
from app.core.logging import configure_logging
from app.main import _default_gateway
from app.services.dolphin.client import default_dolphin_client
from app.services.publications.storage import LocalImageStorage
from app.services.whatsapp.client import default_whatsapp_client
from app.workers import runtime
from app.workers.jobs import maintenance_jobs, messaging_job_objects

log = logging.getLogger("permutados.worker")


def build_jobs():
    gateway = _default_gateway()
    return maintenance_jobs(gateway) + messaging_job_objects(
        dolphin=default_dolphin_client(),
        wa=default_whatsapp_client(),
        gateway=gateway,
        storage=LocalImageStorage(),
    )


def main() -> None:
    configure_logging()
    worker_id = runtime.new_worker_id()
    started_at = utcnow()
    jobs = build_jobs()
    names = [j.name for j in jobs]
    runtime.heartbeat(worker_id, started_at=started_at, info={"jobs": names})
    runtime.recover_on_startup(worker_id)
    for j in jobs:
        if j.record_idle:
            runtime.run_job(j, worker_id)  # reconciliation pass at start-up

    sched = BlockingScheduler(
        timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
    )
    for j in jobs:
        sched.add_job(runtime.run_job, "interval", args=[j, worker_id], seconds=j.interval, id=j.name, name=j.name)
    sched.add_job(
        runtime.heartbeat,
        "interval",
        args=[worker_id],
        kwargs={"started_at": started_at, "info": {"jobs": names}},
        seconds=runtime.HEARTBEAT_SECONDS,
        id="heartbeat",
        name="heartbeat",
    )

    def stop(signum, _frame):
        log.info("signal %s: stopping worker %s", signum, worker_id)
        sched.shutdown(wait=False)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("worker %s started with %d jobs", worker_id, len(sched.get_jobs()))
    try:
        sched.start()
    finally:
        runtime.mark_stopped(worker_id)
        log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
