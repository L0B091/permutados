"""WhatsApp Cloud API sender interface (official Graph API). PHASE 1: interface + minimal client, not yet
exercised against Meta (no credentials). Phase 2 adds templates, media download and the outbox worker."""

from __future__ import annotations

from typing import Protocol

import httpx

from app.core.config import get_settings
from app.core.errors import ExternalServiceError


class WhatsAppSender(Protocol):
    def send_text(self, to_wa_id: str, body: str) -> str: ...


class CloudApiSender:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        s = get_settings()
        token = s.whatsapp_access_token.get_secret_value()
        if not token or not s.whatsapp_phone_number_id:
            raise ExternalServiceError("WhatsApp is not configured", code="WA_NOT_CONFIGURED")
        self._url = f"https://graph.facebook.com/{s.whatsapp_api_version}/{s.whatsapp_phone_number_id}/messages"
        self._client = httpx.Client(timeout=15, transport=transport, headers={"Authorization": f"Bearer {token}"})

    def send_text(self, to_wa_id: str, body: str) -> str:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_wa_id,
            "type": "text",
            "text": {"preview_url": False, "body": body[:4096]},
        }
        try:
            r = self._client.post(self._url, json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ExternalServiceError("WhatsApp send failed", code="WA_SEND_FAILED") from e
        return str(r.json()["messages"][0]["id"])


class RecordingSender:
    """Test/dev sender that records messages instead of calling Meta."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, to_wa_id: str, body: str) -> str:
        self.sent.append((to_wa_id, body))
        return f"wamid.test.{len(self.sent)}"
