import json
from pathlib import Path

from app.services.dolphin.tools import ALLOWED_TOOLS, FORBIDDEN_TOOLS, TOOLS, openai_tools

SPEC_TOOLS = {
    "create_publication",
    "get_my_publications",
    "get_publication",
    "update_publication",
    "delete_publication",
    "search_publications",
    "get_search_page",
    "select_publication",
    "create_offer",
    "get_offer",
    "cancel_offer",
    "accept_offer",
    "reject_offer",
    "get_transaction",
    "create_payment",
    "get_payment_status",
    "send_message",
    "warn_user",
    "report_publication",
    "get_user_context",
}


def test_registry_is_exactly_the_spec_allowlist():
    assert set(TOOLS) == ALLOWED_TOOLS == SPEC_TOOLS
    assert not FORBIDDEN_TOOLS & set(TOOLS)
    assert {t["function"]["name"] for t in openai_tools()} == SPEC_TOOLS


def test_no_tool_accepts_identity_or_state_arguments():
    for t in openai_tools():
        props = set(t["function"]["parameters"].get("properties", {}))
        assert not props & {"user_id", "owner_id", "bidder_id", "status", "amount", "whatsapp_id", "phone"}, t
        assert t["function"]["parameters"].get("additionalProperties") is False


def test_exported_tools_json_in_sync():
    path = Path(__file__).resolve().parents[3] / "dolphin" / "tools" / "tools.json"
    assert json.loads(path.read_text()) == openai_tools(), "run scripts/export_dolphin_tools.py"
