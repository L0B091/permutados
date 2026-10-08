"""WhatsApp Cloud API client (official Graph API) behind the ``WhatsAppClient`` interface.

Implemented operations (https://developers.facebook.com/docs/whatsapp/cloud-api):
  POST /{phone-number-id}/messages   text, image, template, mark-as-read
  GET  /{media-id}                   media metadata (url, mime_type, sha256, file_size)
  GET  <media url>                   media bytes (requires the same bearer token)

Retries: exponential backoff with jitter (injectable ``sleep`` for tests).
* Always retried: connection errors (request never reached Meta), HTTP 429 and Meta throttling codes.
* Idempotent GETs and mark-as-read are also retried on 5xx/timeouts.
* Sends are NOT retried on 5xx/read timeouts inside the client because Meta may already have accepted the
  message; the error is raised as ``retryable`` and the outbox retries later (at-least-once delivery, see
  ``notifications.outbox``). Every send carries ``biz_opaque_callback_data`` = outbox idempotency key, which Meta
  echoes in status webhooks, so deliveries can be correlated.

NOT exercised against Meta in this repository (no credentials). Request building, error mapping, retries and
the 24h-window fallback are unit-tested with ``httpx.MockTransport``.
"""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.config import get_settings
from app.core.errors import ExternalServiceError

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com"

# Meta error codes (https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes)
OUTSIDE_WINDOW_CODES = {131047}  # "Re-engagement message": more than 24h since the user's last message
THROTTLING_CODES = {4, 80007, 130429, 131048, 131056, 133016}
AUTH_CODES = {0, 190}


class WhatsAppError(ExternalServiceError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "WA_ERROR",
        meta_code: int | None = None,
        http_status: int | None = None,
        retryable: bool = False,
        outside_window: bool = False,
    ):
        super().__init__(message, code=code)
        self.meta_code = meta_code
        self.http_status = http_status
        self.retryable = retryable
        self.outside_window = outside_window


@dataclass(frozen=True)
class MediaFile:
    data: bytes
    mime_type: str
    sha256: str | None = None


class WhatsAppClient(Protocol):
    def send_text(self, to: str, body: str, *, idempotency_key: str | None = None) -> str: ...

    def send_image(
        self,
        to: str,
        *,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...

    def send_template(
        self,
        to: str,
        name: str,
        *,
        language: str,
        body_params: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...

    def mark_read(self, message_id: str) -> None: ...

    def download_media(self, media_id: str, *, max_bytes: int) -> MediaFile: ...


_TEMPLATE_PARAM_WS = re.compile(r"[\t\n\r]+")
_TEMPLATE_PARAM_SPACES = re.compile(r" {4,}")


def clean_template_param(value: str, limit: int = 1000) -> str:
    """Meta rejects template parameters with newlines/tabs or more than 4 consecutive spaces."""
    v = _TEMPLATE_PARAM_WS.sub(" ", value or "")
    v = _TEMPLATE_PARAM_SPACES.sub("   ", v).strip()
    return v[:limit] or "-"


def build_text_payload(to: str, body: str, idempotency_key: str | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},
    }
    if idempotency_key:
        p["biz_opaque_callback_data"] = idempotency_key[:512]
    return p


def build_image_payload(
    to: str,
    *,
    link: str | None,
    media_id: str | None,
    caption: str | None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if bool(link) == bool(media_id):
        raise ValueError("exactly one of link or media_id is required")
    image: dict[str, Any] = {"link": link} if link else {"id": media_id}
    if caption:
        image["caption"] = caption[:1024]
    p: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "image",
        "image": image,
    }
    if idempotency_key:
        p["biz_opaque_callback_data"] = idempotency_key[:512]
    return p


def build_template_payload(
    to: str, name: str, *, language: str, body_params: list[str] | None = None, idempotency_key: str | None = None
) -> dict[str, Any]:
    template: dict[str, Any] = {"name": name, "language": {"code": language}}
    if body_params:
        template["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": clean_template_param(v)} for v in body_params]}
        ]
    p: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "template",
        "template": template,
    }
    if idempotency_key:
        p["biz_opaque_callback_data"] = idempotency_key[:512]
    return p


def build_mark_read_payload(message_id: str) -> dict[str, Any]:
    return {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}


def error_from_response(r: httpx.Response) -> WhatsAppError:
    meta_code: int | None = None
    msg = f"WhatsApp API error (HTTP {r.status_code})"
    try:
        err = r.json().get("error") or {}
        meta_code = int(err["code"]) if "code" in err else None
    except (ValueError, TypeError, KeyError, AttributeError):
        err = {}
    if meta_code in OUTSIDE_WINDOW_CODES:
        return WhatsAppError(
            "outside the 24h customer-service window",
            code="WA_OUTSIDE_WINDOW",
            meta_code=meta_code,
            http_status=r.status_code,
            outside_window=True,
        )
    retryable = r.status_code == 429 or r.status_code >= 500 or meta_code in THROTTLING_CODES
    code = "WA_AUTH" if (r.status_code == 401 or meta_code in AUTH_CODES) else "WA_ERROR"
    return WhatsAppError(msg, code=code, meta_code=meta_code, http_status=r.status_code, retryable=retryable)


@dataclass
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    sleep: Callable[[float], None] = time.sleep

    def delay(self, attempt: int) -> float:
        return min(self.max_delay, self.base_delay * (2**attempt)) * (0.5 + random.random() / 2)  # noqa: S311


class CloudApiClient:
    def __init__(
        self,
        *,
        access_token: str | None = None,
        phone_number_id: str | None = None,
        api_version: str | None = None,
        transport: httpx.BaseTransport | None = None,
        retry: RetryPolicy | None = None,
        timeout: float = 15.0,
    ):
        s = get_settings()
        token = access_token or s.whatsapp_access_token.get_secret_value()
        self.phone_number_id = phone_number_id or s.whatsapp_phone_number_id
        if not token or not self.phone_number_id:
            raise ExternalServiceError("WhatsApp is not configured", code="WA_NOT_CONFIGURED")
        self.version = api_version or s.whatsapp_api_version
        self.retry = retry or RetryPolicy(max_retries=s.whatsapp_max_retries)
        self._http = httpx.Client(
            timeout=timeout, transport=transport, headers={"Authorization": f"Bearer {token}"}, follow_redirects=False
        )

    # ---- transport with retries -----------------------------------------------------------------
    def _request(self, method: str, url: str, *, idempotent: bool, **kw: Any) -> httpx.Response:
        attempt = 0
        while True:
            try:
                r = self._http.request(method, url, **kw)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                err = WhatsAppError("WhatsApp unreachable", code="WA_UNREACHABLE", retryable=True)
                cause: Exception = e
            except httpx.TransportError as e:  # read timeout etc.: the request may have been processed
                err = WhatsAppError("WhatsApp timeout", code="WA_TIMEOUT", retryable=True)
                cause = e
                if not idempotent:
                    raise err from e
            else:
                if r.status_code < 400:
                    return r
                err = error_from_response(r)
                cause = None  # type: ignore[assignment]
                throttled = r.status_code == 429 or err.meta_code in THROTTLING_CODES
                if not err.retryable or (not idempotent and not throttled):
                    raise err
            if attempt >= self.retry.max_retries:
                raise err from cause
            self.retry.sleep(self.retry.delay(attempt))
            attempt += 1

    def _messages_url(self) -> str:
        return f"{GRAPH}/{self.version}/{self.phone_number_id}/messages"

    def _send(self, payload: dict[str, Any]) -> str:
        r = self._request("POST", self._messages_url(), idempotent=False, json=payload)
        try:
            return str(r.json()["messages"][0]["id"])
        except (ValueError, KeyError, IndexError, TypeError):
            raise WhatsAppError("unexpected WhatsApp response", code="WA_BAD_RESPONSE") from None

    # ---- interface --------------------------------------------------------------------------------
    def send_text(self, to: str, body: str, *, idempotency_key: str | None = None) -> str:
        return self._send(build_text_payload(to, body, idempotency_key))

    def send_image(
        self,
        to: str,
        *,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        return self._send(
            build_image_payload(to, link=link, media_id=media_id, caption=caption, idempotency_key=idempotency_key)
        )

    def send_template(
        self,
        to: str,
        name: str,
        *,
        language: str,
        body_params: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        return self._send(
            build_template_payload(
                to, name, language=language, body_params=body_params, idempotency_key=idempotency_key
            )
        )

    def mark_read(self, message_id: str) -> None:
        self._request("POST", self._messages_url(), idempotent=True, json=build_mark_read_payload(message_id))

    def download_media(self, media_id: str, *, max_bytes: int) -> MediaFile:
        if not re.fullmatch(r"[0-9A-Za-z_.-]{1,128}", media_id or ""):
            raise WhatsAppError("invalid media id", code="WA_BAD_MEDIA_ID")
        meta = self._request("GET", f"{GRAPH}/{self.version}/{media_id}", idempotent=True).json()
        url, mime = meta.get("url"), meta.get("mime_type") or "application/octet-stream"
        size = int(meta.get("file_size") or 0)
        if not url or not str(url).startswith("https://"):
            raise WhatsAppError("media url missing", code="WA_BAD_RESPONSE")
        if size and size > max_bytes:
            raise WhatsAppError("media too large", code="WA_MEDIA_TOO_LARGE")
        r = self._request("GET", url, idempotent=True)
        data = r.content
        if len(data) > max_bytes:
            raise WhatsAppError("media too large", code="WA_MEDIA_TOO_LARGE")
        sha = meta.get("sha256")
        if sha and hashlib.sha256(data).hexdigest() != sha:
            raise WhatsAppError("media checksum mismatch", code="WA_MEDIA_CHECKSUM", retryable=True)
        return MediaFile(data=data, mime_type=mime, sha256=sha)


@dataclass
class RecordingWhatsAppClient:
    """In-memory client for tests and local development. Records every call; failures can be scripted."""

    sent: list[dict[str, Any]] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    media: dict[str, MediaFile] = field(default_factory=dict)
    # Recipients for whom a free-form send fails with 131047 (simulates Meta's view of the window).
    outside_window_for: set[str] = field(default_factory=set)
    fail_next: list[WhatsAppError] = field(default_factory=list)

    def _maybe_fail(self) -> None:
        if self.fail_next:
            raise self.fail_next.pop(0)

    def _record(self, kind: str, to: str, **data: Any) -> str:
        self.sent.append({"type": kind, "to": to, **data})
        return f"wamid.test.{len(self.sent)}"

    def send_text(self, to: str, body: str, *, idempotency_key: str | None = None) -> str:
        self._maybe_fail()
        if to in self.outside_window_for:
            raise WhatsAppError("outside window", code="WA_OUTSIDE_WINDOW", meta_code=131047, outside_window=True)
        build_text_payload(to, body, idempotency_key)  # same validation as the real client
        return self._record("text", to, body=body, key=idempotency_key)

    def send_image(
        self,
        to: str,
        *,
        link: str | None = None,
        media_id: str | None = None,
        caption: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        self._maybe_fail()
        build_image_payload(to, link=link, media_id=media_id, caption=caption)
        return self._record("image", to, link=link, media_id=media_id, caption=caption, key=idempotency_key)

    def send_template(
        self,
        to: str,
        name: str,
        *,
        language: str,
        body_params: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        self._maybe_fail()
        payload = build_template_payload(to, name, language=language, body_params=body_params)
        params = [p["text"] for c in payload["template"].get("components", []) for p in c["parameters"]]
        return self._record("template", to, name=name, language=language, params=params, key=idempotency_key)

    def mark_read(self, message_id: str) -> None:
        self.read.append(message_id)

    def download_media(self, media_id: str, *, max_bytes: int) -> MediaFile:
        self._maybe_fail()
        f = self.media.get(media_id)
        if f is None:
            raise WhatsAppError("media not found", code="WA_ERROR", http_status=404)
        if len(f.data) > max_bytes:
            raise WhatsAppError("media too large", code="WA_MEDIA_TOO_LARGE")
        return f

    def texts_to(self, to: str) -> list[str]:
        return [m["body"] for m in self.sent if m["to"] == to and m["type"] == "text"]


def default_whatsapp_client() -> WhatsAppClient | None:
    """Real client when configured, otherwise None (the worker then leaves the outbox untouched)."""
    try:
        return CloudApiClient()
    except ExternalServiceError:
        return None
