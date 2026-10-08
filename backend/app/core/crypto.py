"""Field-level encryption for PII (WhatsApp numbers, profile names, blocked message evidence, rendered
contact-release messages).

* AES-256-GCM with a random 96-bit nonce. The column/purpose name is bound as associated data, so a ciphertext
  copied into another column (or another purpose) fails to decrypt.
* Token format (stable, also used by migration 0002): ``v1.<kid>.<urlsafe-b64(nonce || ciphertext+tag)>`` where
  ``kid`` identifies the key, enabling rotation (old keys in ENCRYPTION_OLD_KEYS decrypt, the current key
  encrypts).
* Lookups use a *blind index*: HMAC-SHA256 with a sub-key derived (HKDF) from the current key. Equality search
  without storing the plaintext. Rotating the key requires re-indexing (scripts/rotate_encryption_key.py).

Decryption of WhatsApp numbers is only reachable through ``app.services.users.pii`` (see the architecture test
``tests/security/test_pii_encryption.py``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from dataclasses import dataclass
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = "v1"


class CryptoError(Exception):
    pass


def parse_key(raw: str) -> bytes:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("is required (urlsafe base64 of 32 random bytes)")
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        raise ValueError("must be urlsafe base64") from None
    if len(key) != 32:
        raise ValueError("must decode to exactly 32 bytes")
    return key


def generate_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


def _hkdf(key: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"permutados-pii", info=info).derive(key)


@dataclass(frozen=True)
class _Key:
    kid: str
    aead: AESGCM
    index_key: bytes


def _make(key: bytes) -> _Key:
    return _Key(
        kid=hashlib.sha256(b"kid" + key).hexdigest()[:8],
        aead=AESGCM(_hkdf(key, b"aead-v1")),
        index_key=_hkdf(key, b"blind-index-v1"),
    )


class Cipher:
    def __init__(self, current: str, old: str = ""):
        self.current = _make(parse_key(current))
        self.keys = {self.current.kid: self.current}
        for raw in (k for k in (old or "").split(",") if k.strip()):
            k = _make(parse_key(raw))
            self.keys.setdefault(k.kid, k)

    def encrypt(self, plaintext: str, purpose: str) -> str:
        nonce = os.urandom(12)
        ct = self.current.aead.encrypt(nonce, plaintext.encode("utf-8"), purpose.encode())
        return f"{VERSION}.{self.current.kid}.{base64.urlsafe_b64encode(nonce + ct).decode()}"

    def decrypt(self, token: str, purpose: str) -> str:
        try:
            version, kid, data = token.split(".", 2)
        except (AttributeError, ValueError):
            raise CryptoError("malformed ciphertext") from None
        key = self.keys.get(kid)
        if version != VERSION or key is None:
            raise CryptoError("unknown key or version")
        try:
            blob = base64.urlsafe_b64decode(data)
            return key.aead.decrypt(blob[:12], blob[12:], purpose.encode()).decode("utf-8")
        except (InvalidTag, binascii.Error, ValueError):
            raise CryptoError("decryption failed") from None

    def needs_rotation(self, token: str) -> bool:
        return not token.startswith(f"{VERSION}.{self.current.kid}.")

    def blind_index(self, value: str, purpose: str) -> str:
        return hmac.new(self.current.index_key, f"{purpose}\x00{value}".encode(), hashlib.sha256).hexdigest()


@lru_cache
def get_cipher() -> Cipher:
    from app.core.config import get_settings

    s = get_settings()
    try:
        return Cipher(s.encryption_key.get_secret_value(), s.encryption_old_keys.get_secret_value())
    except ValueError as e:
        raise CryptoError(f"ENCRYPTION_KEY {e}") from None


# Purposes (associated data). Never reuse a purpose for a different kind of value.
WA_ID = "users.whatsapp_id"
DISPLAY_NAME = "users.display_name"
MESSAGE_EVIDENCE = "messages.blocked_body"
NOTIFICATION_BODY = "notifications.body"
INBOUND_ENVELOPE = "webhook_events.whatsapp_envelope"


def encrypt(plaintext: str, purpose: str) -> str:
    return get_cipher().encrypt(plaintext, purpose)


def blind_index(value: str, purpose: str) -> str:
    return get_cipher().blind_index(value, purpose)
