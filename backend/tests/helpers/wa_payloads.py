"""Builders for WhatsApp Cloud API webhook payloads (shape of real Meta deliveries)."""

from __future__ import annotations

import itertools
import time
from typing import Any

_ids = itertools.count(1)


def _envelope(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "5491100000000", "phone_number_id": "PNID"},
                            **value,
                        },
                    }
                ],
            }
        ],
    }


def message(wa_id: str, mtype: str, body: dict[str, Any], *, name: str = "Usuario", msg_id: str | None = None):
    mid = msg_id or f"wamid.TEST{next(_ids):06d}"
    m = {"from": wa_id, "id": mid, "timestamp": str(int(time.time())), "type": mtype, mtype: body}
    return _envelope({"contacts": [{"profile": {"name": name}, "wa_id": wa_id}], "messages": [m]}), mid


def text(wa_id: str, body: str, **kw):
    return message(wa_id, "text", {"body": body}, **kw)


def image(wa_id: str, media_id: str, caption: str | None = None, **kw):
    img = {"id": media_id, "mime_type": "image/jpeg", "sha256": "x"}
    if caption:
        img["caption"] = caption
    return message(wa_id, "image", img, **kw)


def location(wa_id: str, **kw):
    return message(wa_id, "location", {"latitude": -34.58, "longitude": -58.42, "name": "Casa"}, **kw)


def contacts(wa_id: str, phone: str, **kw):
    return message(
        wa_id, "contacts", [{"name": {"formatted_name": "Juan"}, "phones": [{"phone": phone, "type": "CELL"}]}], **kw
    )


def audio(wa_id: str, **kw):
    return message(wa_id, "audio", {"id": "AUDIO1", "mime_type": "audio/ogg"}, **kw)


def status(
    provider_message_id: str,
    status_value: str,
    recipient: str,
    error_code: int | None = None,
    callback_data: str | None = None,
):
    st: dict[str, Any] = {
        "id": provider_message_id,
        "status": status_value,
        "timestamp": str(int(time.time())),
        "recipient_id": recipient,
    }
    if error_code:
        st["errors"] = [{"code": error_code, "title": "error"}]
    if callback_data:
        st["biz_opaque_callback_data"] = callback_data
    return _envelope({"statuses": [st]})
