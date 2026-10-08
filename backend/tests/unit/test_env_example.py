"""`.env.example` documents every setting the backend reads (Definition of Done: complete .env.example) and holds
no real-looking secret."""

import re
from pathlib import Path

from app.core.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"


def _keys_and_values():
    out = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).split("#")[0].strip()
    return out


def test_every_setting_is_documented():
    documented = set(_keys_and_values())
    missing = sorted(k.upper() for k in Settings.model_fields if k.upper() not in documented)
    assert not missing, f"add to .env.example: {missing}"


def test_no_secret_values_in_template():
    values = _keys_and_values()
    for key in (
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_APP_SECRET",
        "WHATSAPP_VERIFY_TOKEN",
        "MERCADOPAGO_ACCESS_TOKEN",
        "MERCADOPAGO_WEBHOOK_SECRET",
        "DOLPHIN_API_KEY",
        "ENCRYPTION_KEY",
        "ADMIN_AUTH_SECRET",
    ):
        assert values[key] == "", f"{key} must be empty in .env.example"
    assert values["POSTGRES_PASSWORD"] == "change-me"
