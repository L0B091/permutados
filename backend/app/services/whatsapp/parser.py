"""Parse WhatsApp Cloud API webhook payloads (entry[].changes[].value.messages[] / statuses[])."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SUPPORTED_TYPES = {"text", "image", "interactive", "button", "location", "contacts"}
IGNORED_TYPES = {"reaction", "system", "ephemeral", "request_welcome"}


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


@dataclass(frozen=True)
class StatusUpdate:
    message_id: str
    status: str  # sent | delivered | read | failed
    recipient: str | None
    error_code: int | None
    callback_data: str | None = None  # biz_opaque_callback_data echoed by Meta (our idempotency key)


def _contacts_text(contacts: list[dict[str, Any]]) -> str:
    """A shared contact card IS contact data: render it so anti-evasion sees the numbers/emails."""
    parts: list[str] = []
    for c in contacts or []:
        parts += [p.get("phone") or p.get("wa_id") or "" for p in c.get("phones") or []]
        parts += [e.get("email") or "" for e in c.get("emails") or []]
        parts += [u.get("url") or "" for u in c.get("urls") or []]
    return "[contacto compartido] " + " ".join(p for p in parts if p)


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
                if mtype in IGNORED_TYPES:
                    continue
                text = None
                media: tuple[str, ...] = ()
                mime = None
                if mtype == "text":
                    text = (m.get("text") or {}).get("body")
                elif mtype == "image":
                    img = m.get("image") or {}
                    media = (str(img["id"]),) if img.get("id") else ()
                    mime = img.get("mime_type")
                    text = img.get("caption")
                elif mtype == "interactive":
                    inter = m.get("interactive") or {}
                    reply = inter.get("button_reply") or inter.get("list_reply") or {}
                    text = reply.get("title") or reply.get("id")
                elif mtype == "button":
                    text = (m.get("button") or {}).get("text")
                elif mtype == "contacts":
                    text = _contacts_text(m.get("contacts") or [])
                try:
                    ts = int(m.get("timestamp") or 0)
                except (TypeError, ValueError):
                    ts = 0
                if not m.get("id") or not m.get("from"):
                    continue
                out.append(
                    InboundMessage(
                        message_id=str(m.get("id")),
                        wa_id=str(m.get("from")),
                        timestamp=ts,
                        type=mtype if mtype in SUPPORTED_TYPES else "unsupported",
                        text=text,
                        media_ids=media,
                        media_mime=mime,
                        profile_name=names.get(m.get("from")),
                    )
                )
    return out


def parse_statuses(payload: dict[str, Any]) -> list[StatusUpdate]:
    out: list[StatusUpdate] = []
    if payload.get("object") != "whatsapp_business_account":
        return out
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for st in (change.get("value") or {}).get("statuses") or []:
                code = None
                errors = st.get("errors") or []
                if errors:
                    try:
                        code = int(errors[0].get("code"))
                    except (TypeError, ValueError):
                        code = None
                if st.get("id") and st.get("status"):
                    cb = st.get("biz_opaque_callback_data")
                    out.append(
                        StatusUpdate(
                            str(st["id"]),
                            str(st["status"]),
                            st.get("recipient_id"),
                            code,
                            str(cb)[:512] if isinstance(cb, str) and cb else None,
                        )
                    )
    return out
