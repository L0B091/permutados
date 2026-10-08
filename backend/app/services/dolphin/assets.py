from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings


def assets_dir() -> Path:
    configured = get_settings().dolphin_assets_path
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[4] / "dolphin"


@lru_cache
def system_prompt() -> str:
    return (assets_dir() / "prompts" / "system.md").read_text(encoding="utf-8")


@lru_cache
def policies() -> str:
    return (assets_dir() / "policies" / "policies.md").read_text(encoding="utf-8")
