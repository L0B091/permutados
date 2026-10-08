"""Real CloudApiClient against httpx.MockTransport: request building, error mapping, retries, media download."""

import hashlib
import json

import httpx
import pytest

from app.services.whatsapp.client import CloudApiClient, RetryPolicy, WhatsAppError, clean_template_param

BASE = "https://graph.facebook.com/v23.0"


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make(responses, *, retries=3):
    rec = Recorder(responses)
    sleeps: list[float] = []
    c = CloudApiClient(
        access_token="tok-test",
        phone_number_id="PNID",
        api_version="v23.0",
        transport=httpx.MockTransport(rec),
        retry=RetryPolicy(max_retries=retries, sleep=sleeps.append),
    )
    return c, rec, sleeps


def ok(mid="wamid.OK"):
    return httpx.Response(200, json={"messaging_product": "whatsapp", "messages": [{"id": mid}]})


def meta_error(status, code):
    return httpx.Response(status, json={"error": {"message": "x", "type": "OAuthException", "code": code}})


def test_send_text_request_shape():
    c, rec, _ = make([ok("wamid.1")])
    assert c.send_text("5491100000001", "hola", idempotency_key="reply:1:0") == "wamid.1"
    req = rec.requests[0]
    assert req.method == "POST" and str(req.url) == f"{BASE}/PNID/messages"
    assert req.headers["authorization"] == "Bearer tok-test"
    body = json.loads(req.content)
    assert body == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "5491100000001",
        "type": "text",
        "text": {"preview_url": False, "body": "hola"},
        "biz_opaque_callback_data": "reply:1:0",
    }


def test_send_template_and_image_shapes():
    c, rec, _ = make([ok(), ok()])
    c.send_template("549110", "permutados_novedad", language="es_AR", body_params=["línea 1\nlínea     2"])
    t = json.loads(rec.requests[0].content)["template"]
    assert t["name"] == "permutados_novedad" and t["language"] == {"code": "es_AR"}
    assert t["components"][0]["parameters"] == [{"type": "text", "text": "línea 1 línea   2"}]
    c.send_image("549110", link="https://cdn.example/x.jpg", caption="foto")
    assert json.loads(rec.requests[1].content)["image"] == {"link": "https://cdn.example/x.jpg", "caption": "foto"}
    with pytest.raises(ValueError):
        c.send_image("549110")


def test_mark_read_shape():
    c, rec, _ = make([httpx.Response(200, json={"success": True})])
    c.mark_read("wamid.IN")
    assert json.loads(rec.requests[0].content) == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.IN",
    }


def test_outside_window_error_is_flagged_not_retried():
    c, rec, sleeps = make([meta_error(400, 131047)])
    with pytest.raises(WhatsAppError) as ei:
        c.send_text("549110", "hola")
    assert ei.value.outside_window and not ei.value.retryable and len(rec.requests) == 1 and not sleeps


def test_throttling_is_retried_with_backoff_for_sends():
    c, rec, sleeps = make([httpx.Response(429, json={}), meta_error(400, 130429), ok("wamid.after")])
    assert c.send_text("549110", "hola") == "wamid.after"
    assert len(rec.requests) == 3 and len(sleeps) == 2 and sleeps[1] >= sleeps[0] * 0.5


def test_send_not_retried_on_5xx_but_flagged_retryable():
    c, rec, _ = make([httpx.Response(500, json={})])
    with pytest.raises(WhatsAppError) as ei:
        c.send_text("549110", "hola")
    assert ei.value.retryable and len(rec.requests) == 1  # Meta may have accepted it: outbox retries later


def test_send_retried_on_connect_error():
    c, rec, _ = make([httpx.ConnectError("down"), ok("wamid.2")])
    assert c.send_text("549110", "hola") == "wamid.2" and len(rec.requests) == 2


def test_read_timeout_on_send_is_not_retried():
    c, rec, _ = make([httpx.ReadTimeout("slow")])
    with pytest.raises(WhatsAppError) as ei:
        c.send_text("549110", "hola")
    assert ei.value.retryable and len(rec.requests) == 1


def test_permanent_errors_and_auth():
    c, _, _ = make([meta_error(400, 131026)])
    with pytest.raises(WhatsAppError) as ei:
        c.send_text("549110", "hola")
    assert not ei.value.retryable and ei.value.meta_code == 131026
    c, _, _ = make([meta_error(401, 190)])
    with pytest.raises(WhatsAppError) as ei:
        c.send_text("549110", "hola")
    assert ei.value.code == "WA_AUTH"


def test_retries_exhausted():
    c, rec, sleeps = make([httpx.Response(429, json={})] * 3, retries=2)
    with pytest.raises(WhatsAppError):
        c.send_text("549110", "hola")
    assert len(rec.requests) == 3 and len(sleeps) == 2


def test_download_media_two_steps_with_auth_and_checksum():
    data = b"\xff\xd8\xff" + b"0" * 5000
    meta = {
        "url": "https://lookaside.fbsbx.com/media/abc",
        "mime_type": "image/jpeg",
        "sha256": hashlib.sha256(data).hexdigest(),
        "file_size": len(data),
        "id": "MEDIA1",
    }
    c, rec, _ = make([httpx.Response(500), httpx.Response(200, json=meta), httpx.Response(200, content=data)])
    f = c.download_media("MEDIA1", max_bytes=10_000)
    assert f.data == data and f.mime_type == "image/jpeg"
    assert str(rec.requests[1].url) == f"{BASE}/MEDIA1"  # GET retried after the 500
    assert str(rec.requests[2].url) == meta["url"]
    assert all(r.headers["authorization"] == "Bearer tok-test" for r in rec.requests)


def test_download_media_limits_and_checksum():
    big = {"url": "https://x/m", "mime_type": "image/jpeg", "file_size": 50_000_000}
    c, _, _ = make([httpx.Response(200, json=big)])
    with pytest.raises(WhatsAppError) as ei:
        c.download_media("MEDIA1", max_bytes=10_000)
    assert ei.value.code == "WA_MEDIA_TOO_LARGE"
    meta = {"url": "https://x/m", "mime_type": "image/jpeg", "sha256": "0" * 64}
    c, _, _ = make([httpx.Response(200, json=meta), httpx.Response(200, content=b"abc")])
    with pytest.raises(WhatsAppError) as ei:
        c.download_media("MEDIA1", max_bytes=10_000)
    assert ei.value.code == "WA_MEDIA_CHECKSUM"
    c, rec, _ = make([])
    with pytest.raises(WhatsAppError):
        c.download_media("../../etc/passwd", max_bytes=10)
    assert not rec.requests
    meta = {"url": "http://insecure/m", "mime_type": "image/jpeg"}
    c, _, _ = make([httpx.Response(200, json=meta)])
    with pytest.raises(WhatsAppError):
        c.download_media("MEDIA1", max_bytes=10_000)


def test_template_param_cleaning():
    assert clean_template_param("a\n\tb      c") == "a b   c"
    assert clean_template_param("") == "-"
    assert len(clean_template_param("x" * 5000)) == 1000
