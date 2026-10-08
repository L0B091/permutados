import pytest

from app.services.search.query_parser import is_next_page_command, parse_query

ZONES = [("palermo", "palermo"), ("villa-crespo", "villa crespo")]


def test_basic_terms():
    f = parse_query("busco auriculares bluetooth", ZONES)
    assert f.terms == ["auriculares", "bluetooth"] and f.zone_slug is None


def test_zone_price_and_swap():
    f = parse_query("Busco una bici en Villa Crespo hasta 50 mil que acepte permuta", ZONES)
    assert f.zone_slug == "villa-crespo"
    assert f.max_price == "50000"
    assert f.accepts_swap is True
    assert f.terms == ["bici"]


def test_price_with_dots():
    assert parse_query("heladera hasta $120.000", ZONES).max_price == "120000"


@pytest.mark.parametrize("cmd", ["más", "mas", "Siguiente", "ver más", "ver mas!"])
def test_next_page_commands(cmd):
    assert is_next_page_command(cmd)


def test_not_next_page():
    assert not is_next_page_command("más barato que eso")
