"""Mercado Pago webhook signature (x-signature: ``ts=...,v1=<hex>``).

Manifest per official docs: ``id:{data.id};request-id:{x-request-id};ts:{ts};`` where data.id comes from
the query string (lower-cased if alphanumeric) and absent pairs are omitted. HMAC-SHA256 with the webhook
secret, hex, compared in constant time. A timestamp tolerance limits replay of captured requests.
"""

from __future__ import annotations

import hashlib
import hmac
import time


class SignatureError(Exception):
    pass


def parse_signature_header(header: str | None) -> tuple[str, str]:
    if not header:
        raise SignatureError("missing x-signature")
    parts = {}
    for chunk in header.split(","):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip()
    ts, v1 = parts.get("ts"), parts.get("v1")
    if not ts or not v1 or not ts.isdigit():
        raise SignatureError("malformed x-signature")
    return ts, v1


def build_manifest(data_id: str | None, request_id: str | None, ts: str) -> str:
    out = ""
    if data_id:
        out += f"id:{data_id.lower() if data_id.isalnum() else data_id};"
    if request_id:
        out += f"request-id:{request_id};"
    out += f"ts:{ts};"
    return out


def compute_signature(secret: str, data_id: str | None, request_id: str | None, ts: str) -> str:
    return hmac.new(secret.encode(), build_manifest(data_id, request_id, ts).encode(), hashlib.sha256).hexdigest()


def verify_signature(
    *,
    secret: str,
    x_signature: str | None,
    x_request_id: str | None,
    data_id: str | None,
    tolerance_seconds: int | None = 600,
    now: float | None = None,
) -> str:
    """Return ``ts`` on success; raise SignatureError otherwise."""
    if not secret:
        raise SignatureError("webhook secret not configured")
    ts, v1 = parse_signature_header(x_signature)
    expected = compute_signature(secret, data_id, x_request_id, ts)
    if not hmac.compare_digest(expected, v1):
        raise SignatureError("signature mismatch")
    if tolerance_seconds is not None:
        ts_seconds = int(ts) / 1000 if len(ts) >= 13 else int(ts)  # docs show both s and ms
        if abs((now or time.time()) - ts_seconds) > tolerance_seconds:
            raise SignatureError("timestamp out of tolerance (possible replay)")
    return ts
