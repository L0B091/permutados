"""Logging with secret redaction. Tokens/keys are never printed."""

from __future__ import annotations

import logging
import re

from app.core.config import get_settings

_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(
        r"(?i)((?:access_token|refresh_token|token|api_key|apikey|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"
    ),
    re.compile(r"\b(APP_USR|TEST)-[0-9A-Za-z\-]{20,}\b"),  # Mercado Pago token shapes
    re.compile(r"\bEAA[A-Za-z0-9]{20,}\b"),  # Meta access token shape
]


def redact(text: str, secrets: list[str] | None = None) -> str:
    for s in secrets or []:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    for p in _PATTERNS:
        text = p.sub(lambda m: (m.group(1) if m.groups() and m.group(1) else "") + "***", text)
    return text


class RedactingFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._secrets = get_settings().secret_values()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        record.msg = redact(msg, self._secrets)
        record.args = ()
        return True


def configure_logging() -> None:
    settings = get_settings()
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    if not any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler.addFilter(RedactingFilter())
        root.addHandler(handler)
