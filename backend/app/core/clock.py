"""Single source of 'now' so time-dependent rules can be tested deterministically."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
