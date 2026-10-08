"""Parse WhatsApp Cloud API webhook payloads (entry[].changes[].value.messages[])."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    wa_id: str
    timestamp: int
    type: str
    text: str | None
    media_ids: tuple[str, ...]
    media_mime: str | None
    profile_name: str | None


def parse_webhook(payload: dict[str, Any]) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    if payload.get("object") != "whatsapp_business_account":
        return out
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
            for m in value.get("messages") or []:
                mtype = m.get("type", "unknown")
                text = None
                media: tuple[str, ...] = ()
                mime = None
                if mtype == "text":
                    text = (m.get("text") or {}).get("body")
                elif mtype == "image":
                    img = m.get("image") or {}
                    media = (img["id"],) if img.get("id") else ()
                    mime = img.get("mime_type")
                    text = img.get("caption")
                elif mtype == "interactive":
                    inter = m.get("interactive") or {}
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    text = reply.get("title") or reply.get("id")
                elif mtype == "button":
                    text = (m.get("button") or {}).get("text")
                try:
                    ts = int(m.get("timestamp") or 0)
                except (TypeError, ValueError):
                    ts = 0
                out.append(
                    InboundMessage(
                        message_id=str(m.get("id")),
                        wa_id=str(m.get("from")),
                        timestamp=ts,
                        type=mtype,
                        text=text,
                        media_ids=media,
                        media_mime=mime,
                        profile_name=names.get(m.get("from")),
                    )
                )
    return out
