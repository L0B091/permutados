"""Password hashing (Argon2id) and JWT helpers for the admin API."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.clock import utcnow
from app.core.config import get_settings

_hasher = PasswordHasher()
JWT_ALG = "HS256"
JWT_ISSUER = "permutados"


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# A valid hash used to equalise timing when the username does not exist.
DUMMY_PASSWORD_HASH = _hasher.hash(secrets.token_urlsafe(16))


def _secret() -> str:
    s = get_settings().admin_auth_secret.get_secret_value()
    if len(s) < 32:
        raise RuntimeError("ADMIN_AUTH_SECRET is not configured (min 32 chars)")
    return s


def create_access_token(admin_id: int, role: str, *, session_family: str) -> str:
    now = utcnow()
    payload = {
        "sub": str(admin_id),
        "role": role,
        "typ": "access",
        "fam": session_family,
        "iss": JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=get_settings().admin_access_token_minutes)).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _secret(), algorithm=JWT_ALG)


def decode_access_token(token: str) -> dict[str, Any]:
    payload = jwt.decode(
        token,
        _secret(),
        algorithms=[JWT_ALG],
        issuer=JWT_ISSUER,
        options={"require": ["exp", "iat", "sub", "typ", "iss"]},
    )
    if payload.get("typ") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def new_refresh_token() -> str:
    """Opaque refresh token. Only its SHA-256 is stored."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
