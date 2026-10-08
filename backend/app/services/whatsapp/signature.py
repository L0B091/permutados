"""Meta webhook verification: GET handshake and X-Hub-Signature-256 (HMAC-SHA256 of the raw body)."""

from __future__ import annotations

import hashlib
import hmac


def verify_handshake(mode: str | None, token: str | None, expected_token: str) -> bool:
    return (
        bool(expected_token)
        and mode == "subscribe"
        and token is not None
        and hmac.compare_digest(token, expected_token)
    )


def compute_signature(app_secret: str, raw_body: bytes) -> str:
    return "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(compute_signature(app_secret, raw_body), header)
