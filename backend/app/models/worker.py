"""Worker bookkeeping: per-job state, run history and heartbeats (recovery and the admin "worker status")."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import IdMixin, pg_enum
from app.models.enums import JobRunStatus


class JobState(Base):
    """One row per periodic job: last run, last success, consecutive failures, current run (if any)."""

    __tablename__ = "job_state"

    job_name: Mapped[str] = mapped_column(String(60), primary_key=True)
    status: Mapped[JobRunStatus] = mapped_column(pg_enum(JobRunStatus, "job_run_status"), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(80))
    current_run_id: Mapped[int | None] = mapped_column(BigInteger)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_affected: Mapped[int | None] = mapped_column(Integer)
    last_duration_ms: Mapped[int | None] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    runs: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    failures: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class JobRun(IdMixin, Base):
    """History of runs that did something, failed or were abandoned (idle runs only update job_state)."""

    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_job_runs_job_started", "job_name", "started_at"),)

    job_name: Mapped[str] = mapped_column(String(60), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[JobRunStatus] = mapped_column(pg_enum(JobRunStatus, "job_run_status"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    affected: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(120), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    info: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
