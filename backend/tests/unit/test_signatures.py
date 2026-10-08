import time

import pytest

from app.services.mercadopago import signature as mp
from app.services.whatsapp import signature as wa

SECRET = "s3cr3t"


def _header(data_id, req_id, ts):
    return f"ts={ts},v1={mp.compute_signature(SECRET, data_id, req_id, ts)}"


def test_mp_manifest_matches_docs():
    assert mp.build_manifest("123456", "req-1", "1704908010") == "id:123456;request-id:req-1;ts:1704908010;"
    assert mp.build_manifest("ORD01JQ4S4KY8HWQ6NA5PXB65B3D3", None, "1") == "id:ord01jq4s4ky8hwq6na5pxb65b3d3;ts:1;"


def test_mp_valid_signature():
    ts = str(int(time.time()))
    assert mp.verify_signature(secret=SECRET, x_signature=_header("42", "r", ts), x_request_id="r", data_id="42") == ts


def test_mp_ms_timestamp_supported():
    ts = str(int(time.time() * 1000))
    mp.verify_signature(secret=SECRET, x_signature=_header("42", "r", ts), x_request_id="r", data_id="42")


@pytest.mark.parametrize("mutate", ["data_id", "request_id", "secret", "missing"])
def test_mp_invalid_signature(mutate):
    ts = str(int(time.time()))
    header = _header("42", "r", ts)
    kwargs = dict(secret=SECRET, x_signature=header, x_request_id="r", data_id="42")
    if mutate == "data_id":
        kwargs["data_id"] = "43"
    elif mutate == "request_id":
        kwargs["x_request_id"] = "other"
    elif mutate == "secret":
        kwargs["secret"] = "wrong"
    else:
        kwargs["x_signature"] = None
    with pytest.raises(mp.SignatureError):
        mp.verify_signature(**kwargs)


def test_mp_replay_outside_tolerance():
    ts = str(int(time.time()) - 3600)
    with pytest.raises(mp.SignatureError, match="tolerance"):
        mp.verify_signature(
            secret=SECRET, x_signature=_header("42", "r", ts), x_request_id="r", data_id="42", tolerance_seconds=600
        )


def test_whatsapp_signature():
    body = b'{"object":"whatsapp_business_account"}'
    good = wa.compute_signature("app", body)
    assert wa.verify_signature("app", body, good)
    assert not wa.verify_signature("app", body + b" ", good)
    assert not wa.verify_signature("app", body, None)
    assert not wa.verify_signature("", body, good)


def test_whatsapp_handshake():
    assert wa.verify_handshake("subscribe", "tok", "tok")
    assert not wa.verify_handshake("subscribe", "bad", "tok")
    assert not wa.verify_handshake("subscribe", "tok", "")
