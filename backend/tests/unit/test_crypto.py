import pytest

from app.core import crypto
from app.core.crypto import Cipher, CryptoError, generate_key, parse_key

K1, K2 = generate_key(), generate_key()


def test_roundtrip_and_random_nonce():
    c = Cipher(K1)
    a, b = c.encrypt("5491155554444", crypto.WA_ID), c.encrypt("5491155554444", crypto.WA_ID)
    assert a != b and a.startswith("v1.")
    assert "5491155554444" not in a
    assert c.decrypt(a, crypto.WA_ID) == c.decrypt(b, crypto.WA_ID) == "5491155554444"


def test_purpose_is_bound_as_associated_data():
    c = Cipher(K1)
    tok = c.encrypt("5491155554444", crypto.WA_ID)
    with pytest.raises(CryptoError):
        c.decrypt(tok, crypto.NOTIFICATION_BODY)


def test_tampering_and_wrong_key_fail():
    c = Cipher(K1)
    tok = c.encrypt("secreto", crypto.DISPLAY_NAME)
    v, kid, data = tok.split(".", 2)
    flipped = data[:-2] + ("A" if data[-2] != "A" else "B") + data[-1]
    with pytest.raises(CryptoError):
        c.decrypt(f"{v}.{kid}.{flipped}", crypto.DISPLAY_NAME)
    with pytest.raises(CryptoError):
        Cipher(K2).decrypt(tok, crypto.DISPLAY_NAME)
    with pytest.raises(CryptoError):
        c.decrypt("garbage", crypto.DISPLAY_NAME)


def test_rotation_old_keys_decrypt_new_key_encrypts():
    old = Cipher(K1)
    tok = old.encrypt("5491100001111", crypto.WA_ID)
    rotated = Cipher(K2, old=K1)
    assert rotated.decrypt(tok, crypto.WA_ID) == "5491100001111"
    assert rotated.needs_rotation(tok)
    assert not rotated.needs_rotation(rotated.encrypt("x", crypto.WA_ID))


def test_blind_index_deterministic_and_keyed():
    a, b = Cipher(K1), Cipher(K2)
    assert a.blind_index("5491100001111", crypto.WA_ID) == a.blind_index("5491100001111", crypto.WA_ID)
    assert a.blind_index("5491100001111", crypto.WA_ID) != b.blind_index("5491100001111", crypto.WA_ID)
    assert a.blind_index("5491100001111", crypto.WA_ID) != a.blind_index("5491100001112", crypto.WA_ID)
    assert len(a.blind_index("x", crypto.WA_ID)) == 64


@pytest.mark.parametrize("bad", ["", "short", "!!!notbase64!!!", "dG9vLXNob3J0"])
def test_parse_key_rejects_bad_keys(bad):
    with pytest.raises(ValueError):
        parse_key(bad)


def test_production_validation_requires_key():
    from app.core.config import Settings

    s = Settings(environment="production", encryption_key="")
    assert any("ENCRYPTION_KEY" in p for p in s.validate_for_production())
