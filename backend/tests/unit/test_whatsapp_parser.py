from app.services.whatsapp.parser import parse_statuses, parse_webhook
from tests.helpers import wa_payloads as P


def test_text_image_and_profile():
    payload, mid = P.text("5491100000001", "hola", name="Ana")
    [m] = parse_webhook(payload)
    assert (m.message_id, m.wa_id, m.type, m.text, m.profile_name) == (mid, "5491100000001", "text", "hola", "Ana")
    payload, _ = P.image("5491100000001", "MEDIA1", caption="mi bici")
    [m] = parse_webhook(payload)
    assert m.type == "image" and m.media_ids == ("MEDIA1",) and m.text == "mi bici" and m.media_mime == "image/jpeg"


def test_contacts_are_rendered_for_anti_evasion():
    payload, _ = P.contacts("5491100000001", "+54 9 11 5555-4444")
    [m] = parse_webhook(payload)
    assert m.type == "contacts" and "5555-4444" in m.text


def test_location_unsupported_and_reactions():
    [loc] = parse_webhook(P.location("5491100000001")[0])
    assert loc.type == "location" and loc.text is None
    [aud] = parse_webhook(P.audio("5491100000001")[0])
    assert aud.type == "unsupported"
    reaction, _ = P.message("5491100000001", "reaction", {"message_id": "x", "emoji": "👍"})
    assert parse_webhook(reaction) == []


def test_statuses():
    [st] = parse_statuses(P.status("wamid.OUT1", "failed", "5491100000001", 131047))
    assert (st.message_id, st.status, st.error_code) == ("wamid.OUT1", "failed", 131047)
    assert parse_webhook(P.status("wamid.OUT1", "read", "5491100000001")) == []
    assert parse_webhook({"object": "page"}) == []
