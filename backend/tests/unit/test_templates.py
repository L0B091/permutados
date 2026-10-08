from datetime import UTC, datetime

from app.services.notifications import templates as t


def test_money_format():
    assert t.money(3000, "ARS") == "ARS 3.000"
    assert t.money("1500000.50", "ARS") == "ARS 1.500.000,50"


def test_offer_messages_state_24h_to_both_parties():
    exp = datetime(2026, 10, 9, 17, 30, tzinfo=UTC)
    bidder = t.offer_created_bidder(
        item_code="ITEM-AAAAA", title="Bici", summary="ARS 100.000", hours=24, expires_at=exp
    )
    owner = t.offer_received_owner(
        item_code="ITEM-AAAAA", title="Bici", summary="ARS 100.000", message=None, hours=24, expires_at=exp
    )
    assert "Tu oferta vence en 24h" in bidder
    assert "Tenés 24h para aceptar o rechazar, luego la oferta caduca" in owner
    assert "09/10 14:30 (hora Argentina)" in bidder  # 17:30 UTC = 14:30 ART


def test_evasion_warning_announces_penalty():
    assert "próxima vez" in t.EVASION_WARNING and "suspendida" in t.EVASION_WARNING
