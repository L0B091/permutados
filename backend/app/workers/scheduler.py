"""Worker process: ``python -m app.workers.scheduler``.

APScheduler runs the jobs from ``jobs.job_definitions``. On start it runs every job once (recovery after a
restart), then on its interval. Jobs are idempotent and serialised with advisory locks.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from app.core.logging import configure_logging
from app.main import _default_gateway
from app.services.dolphin.client import default_dolphin_client
from app.services.publications.storage import LocalImageStorage
from app.services.whatsapp.client import default_whatsapp_client
from app.workers.jobs import job_definitions, messaging_jobs, run_with_lock

log = logging.getLogger("permutados.worker")


def _wrap(name, fn):
    def run():
        try:
            n = run_with_lock(name, fn)
            if n:
                log.info("job %s affected %s rows", name, n)
        except Exception:
            log.exception("job %s failed", name)

    return run


def main() -> None:
    configure_logging()
    sched = BlockingScheduler(
        timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
    )
    for name, (fn, interval) in job_definitions(_default_gateway()).items():
        job = _wrap(name, fn)
        job()  # recovery run at startup
        sched.add_job(job, "interval", seconds=interval, id=name, name=name, replace_existing=True)

    def plain(name, fn):
        def run():
            try:
                n = fn()
                if n:
                    log.info("job %s handled %s items", name, n)
            except Exception:
                log.exception("job %s failed", name)

        return run

    for name, (fn, interval) in messaging_jobs(
        dolphin=default_dolphin_client(),
        wa=default_whatsapp_client(),
        gateway=_default_gateway(),
        storage=LocalImageStorage(),
    ).items():
        sched.add_job(plain(name, fn), "interval", seconds=interval, id=name, name=name, replace_existing=True)
    log.info("worker started with %d jobs", len(sched.get_jobs()))
    sched.start()


if __name__ == "__main__":
    main()
