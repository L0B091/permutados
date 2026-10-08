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
    head, sig = tok.rsplit(".", 1)
    # flip a character in the middle of the signature (never the last one: its low bits are padding), always to a
    # different value, so the tampered token is guaranteed to differ in the signed bytes
    i = len(sig) // 2
    tampered = f"{head}.{sig[:i]}{'A' if sig[i] != 'A' else 'B'}{sig[i + 1 :]}"
    assert tampered != tok
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(tampered)
    # a modified payload with the original signature is rejected too
    h, payload, _ = tok.split(".")
    other = jwt.utils.base64url_encode(jwt.utils.base64url_decode(payload).replace(b'"7"', b'"1"')).decode()
    assert other != payload
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(f"{h}.{other}.{sig}")
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
