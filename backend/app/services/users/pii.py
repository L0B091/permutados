"""The ONLY place where encrypted PII is turned back into plaintext.

Each reveal function has a single, named purpose so its call sites can be audited (and are pinned by the
architecture test in tests/security/test_pii_encryption.py):

* ``reveal_whatsapp_for_release``   – contact release: the counterpart's number goes into the release message.
* ``reveal_whatsapp_for_delivery``  – the outbox dispatcher needs the recipient address to call WhatsApp.
* ``reveal_notification_body``      – the dispatcher hands an encrypted (sensitive) message to WhatsApp.
* ``reveal_display_name_for_admin`` / ``reveal_message_evidence_for_admin`` – single-user admin views (audited).
* ``open_inbound_envelope``         – the inbound worker reads the queued message text.

Nothing returned here may be logged or persisted in plaintext.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.core import crypto
from app.core.errors import ValidationFailed

_WA_ID = re.compile(r"^[0-9]{6,20}$")


def normalize_whatsapp_id(wa_id: str) -> str:
    wa = re.sub(r"[^\d]", "", wa_id or "")
    if not _WA_ID.match(wa):
        raise ValidationFailed("invalid WhatsApp id", code="INVALID_WHATSAPP_ID")
    return wa


@dataclass(frozen=True)
class WaIdentity:
    """Encrypted identity computed as soon as a WhatsApp number enters the system (webhook)."""

    hash: str
    enc: str
    last4: str

    def as_dict(self) -> dict[str, str]:
        return {"hash": self.hash, "enc": self.enc, "last4": self.last4}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WaIdentity:
        return cls(hash=str(d["hash"]), enc=str(d["enc"]), last4=str(d["last4"]))


def whatsapp_hash(wa_id: str) -> str:
    return crypto.blind_index(normalize_whatsapp_id(wa_id), crypto.WA_ID)


def identity_from_wa_id(wa_id: str) -> WaIdentity:
    wa = normalize_whatsapp_id(wa_id)
    return WaIdentity(hash=crypto.blind_index(wa, crypto.WA_ID), enc=crypto.encrypt(wa, crypto.WA_ID), last4=wa[-4:])


def encrypt_display_name(name: str | None) -> str | None:
    name = (name or "").strip()[:120]
    return crypto.encrypt(name, crypto.DISPLAY_NAME) if name else None


def masked_whatsapp(user: Any) -> str:
    return f"•••{user.whatsapp_last4}"


# ---- reveals -------------------------------------------------------------------------------------
def reveal_whatsapp_for_release(user: Any) -> str:
    return crypto.get_cipher().decrypt(user.whatsapp_enc, crypto.WA_ID)


def reveal_whatsapp_for_delivery(user: Any) -> str:
    return crypto.get_cipher().decrypt(user.whatsapp_enc, crypto.WA_ID)


def seal_notification_body(text: str) -> str:
    return crypto.encrypt(text, crypto.NOTIFICATION_BODY)


def reveal_notification_body(notification: Any) -> str:
    if not notification.sensitive:
        return notification.body
    return crypto.get_cipher().decrypt(notification.body_enc, crypto.NOTIFICATION_BODY)


def seal_message_evidence(text: str) -> str:
    return crypto.encrypt(text, crypto.MESSAGE_EVIDENCE)


def reveal_message_evidence_for_admin(message: Any) -> str | None:
    if not message.body_enc:
        return None
    return crypto.get_cipher().decrypt(message.body_enc, crypto.MESSAGE_EVIDENCE)


def reveal_display_name_for_admin(user: Any) -> str | None:
    if not user.display_name_enc:
        return None
    return crypto.get_cipher().decrypt(user.display_name_enc, crypto.DISPLAY_NAME)


def seal_inbound_envelope(data: dict[str, Any]) -> str:
    return crypto.encrypt(json.dumps(data, ensure_ascii=False), crypto.INBOUND_ENVELOPE)


def open_inbound_envelope(token: str) -> dict[str, Any]:
    return json.loads(crypto.get_cipher().decrypt(token, crypto.INBOUND_ENVELOPE))


# ---- key rotation ------------------------------------------------------------------------------------
def rotate_encryption(session: Any, cipher: crypto.Cipher) -> dict[str, int]:
    """Re-encrypt every PII column with ``cipher.current`` and rebuild the blind index.

    ``cipher`` must know the old keys (ENCRYPTION_OLD_KEYS). Run with the worker stopped and the inbound queue
    drained (queued rows carry identities indexed with the old key). Single transaction: all or nothing.
    """
    from sqlalchemy import text

    counts = {"users": 0, "messages": 0, "notifications": 0}
    for uid, enc, name_enc in session.execute(text("SELECT id, whatsapp_enc, display_name_enc FROM users")).all():
        wa = cipher.decrypt(enc, crypto.WA_ID)
        name = cipher.decrypt(name_enc, crypto.DISPLAY_NAME) if name_enc else None
        session.execute(
            text("UPDATE users SET whatsapp_hash=:h, whatsapp_enc=:e, display_name_enc=:n WHERE id=:id"),
            {
                "h": cipher.blind_index(wa, crypto.WA_ID),
                "e": cipher.encrypt(wa, crypto.WA_ID),
                "n": cipher.encrypt(name, crypto.DISPLAY_NAME) if name else None,
                "id": uid,
            },
        )
        counts["users"] += 1
    for table, purpose in (("messages", crypto.MESSAGE_EVIDENCE), ("notifications", crypto.NOTIFICATION_BODY)):
        rows = session.execute(text(f"SELECT id, body_enc FROM {table} WHERE body_enc IS NOT NULL")).all()  # noqa: S608
        for rid, enc in rows:
            if not cipher.needs_rotation(enc):
                continue
            session.execute(
                text(f"UPDATE {table} SET body_enc=:e WHERE id=:id"),  # noqa: S608
                {"e": cipher.encrypt(cipher.decrypt(enc, purpose), purpose), "id": rid},
            )
            counts[table] += 1
    return counts
