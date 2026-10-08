import jwt
import pytest

from app.core.logging import redact
from app.core.security import create_access_token, decode_access_token, hash_password, verify_password


def test_password_hashing():
    h = hash_password("a-very-long-password")
    assert h.startswith("$argon2id$") and verify_password(h, "a-very-long-password")
    assert not verify_password(h, "wrong-password-123")
    with pytest.raises(ValueError):
        hash_password("short")


def test_access_token_roundtrip_and_tamper():
    tok = create_access_token(7, "ADMIN", session_family="fam")
    assert decode_access_token(tok)["sub"] == "7"
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(tok[:-2] + ("A" if tok[-1] != "A" else "B") + tok[-1])
    forged = jwt.encode(
        {"sub": "1", "typ": "access", "iss": "permutados", "iat": 1, "exp": 9999999999},
        "another-secret-that-is-long-enough-000000",
        algorithm="HS256",
    )
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(forged)


def test_log_redaction():
    out = redact(
        "Authorization: Bearer abc.def.ghi token=XYZ123 APP_USR-1234567890123456789012-x", ["supersecretvalue"]
    )
    assert "abc.def.ghi" not in out and "XYZ123" not in out and "APP_USR-123456" not in out
    assert redact("db pass supersecretvalue", ["supersecretvalue"]) == "db pass ***"
