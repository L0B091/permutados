import pytest
from sqlalchemy import select

from app.core.errors import Ambiguous, InvalidState
from app.models.item import Item
from app.services.offers import service as offers
from app.services.search import service as search


@pytest.fixture
def catalog(make_user, publish_item):
    owner = make_user()
    items = [
        publish_item(owner, title=f"Auriculares bluetooth modelo {i}", category="audio", price=str(10000 + i))
        for i in range(12)
    ]
    publish_item(owner, title="Heladera no frost", category="electro")
    return owner, items


def test_pagination_five_per_page_without_repetition(db, make_user, catalog):
    buyer = make_user()
    p1 = search.start_search(db, buyer, "busco auriculares")
    p2 = search.next_page(db, buyer, p1.search_session_id)
    p3 = search.next_page(db, buyer, p1.search_session_id)
    p4 = search.next_page(db, buyer, p1.search_session_id)
    assert [len(p.results) for p in (p1, p2, p3)] == [5, 5, 2]
    assert p3.exhausted and p4.results == [] and p4.exhausted
    ids = [r["item_id"] for p in (p1, p2, p3) for r in p.results]
    assert len(ids) == len(set(ids)) == 12
    assert all("owner" not in r and "phone" not in r for r in p1.results)  # privacy before acceptance


def test_no_repetition_even_if_new_items_appear(db, make_user, catalog, publish_item):
    buyer = make_user()
    p1 = search.start_search(db, buyer, "auriculares")
    publish_item(catalog[0], title="Auriculares bluetooth nuevos", category="audio")
    p2 = search.next_page(db, buyer, p1.search_session_id)
    assert not {r["item_id"] for r in p1.results} & {r["item_id"] for r in p2.results}


def test_own_reserved_and_other_items_excluded(db, make_user, catalog, publish_item):
    owner, items = catalog
    buyer, other = make_user(), make_user()
    offers.create_offer(db, other, items[0].public_id, offer_type="MONEY", offered_price="9000")
    db.commit()
    ids = set()
    page = search.start_search(db, buyer, "auriculares")
    while page.results:
        ids |= {r["item_id"] for r in page.results}
        page = search.next_page(db, buyer, page.search_session_id)
    assert items[0].public_id not in ids and len(ids) == 11  # reserved (paused) item hidden
    assert search.start_search(db, owner, "auriculares").results == []  # own items hidden


def test_filters_zone_and_price(db, make_user, publish_item):
    owner, buyer = make_user(), make_user()
    publish_item(owner, title="Bicicleta urbana", zone="Palermo", price="100000")
    publish_item(owner, title="Bicicleta montaña", zone="Belgrano", price="300000")
    r = search.start_search(db, buyer, "busco bicicleta en belgrano")
    assert [x["title"] for x in r.results] == ["Bicicleta montaña"]
    r = search.start_search(db, buyer, "bicicleta hasta 150 mil")
    assert [x["title"] for x in r.results] == ["Bicicleta urbana"]


def test_fallback_any_term(db, make_user, publish_item):
    owner, buyer = make_user(), make_user()
    publish_item(owner, title="Guitarra criolla")
    r = search.start_search(db, buyer, "guitarra electrica")
    assert r.used_fallback and r.results[0]["title"] == "Guitarra criolla"


def test_selection_by_number_ordinal_code_and_text(db, make_user, catalog):
    buyer = make_user()
    p1 = search.start_search(db, buyer, "auriculares")
    sid = p1.search_session_id
    assert search.resolve_selection(db, buyer, "2", search_session_id=sid).public_id == p1.results[1]["item_id"]
    assert (
        search.resolve_selection(db, buyer, "el tercero", search_session_id=sid).public_id == p1.results[2]["item_id"]
    )
    code = p1.results[4]["item_id"]
    assert search.resolve_selection(db, buyer, f"quiero el {code}", search_session_id=sid).public_id == code
    with pytest.raises(Ambiguous):
        search.resolve_selection(db, buyer, "los auriculares bluetooth", search_session_id=sid)


def test_selection_by_natural_language(db, make_user, publish_item):
    owner, buyer = make_user(), make_user()
    publish_item(owner, title="Bicicleta roja", category="usados")
    publish_item(owner, title="Bicicleta azul", category="usados")
    publish_item(owner, title="Taladro percutor", category="usados")
    p = search.start_search(db, buyer, "usados")
    sid = p.search_session_id
    assert search.resolve_selection(db, buyer, "quiero el taladro", search_session_id=sid).title == "Taladro percutor"
    assert search.resolve_selection(db, buyer, "la bici azul", search_session_id=sid).title == "Bicicleta azul"
    with pytest.raises(Ambiguous) as e:
        search.resolve_selection(db, buyer, "la bicicleta", search_session_id=sid)
    assert {c["title"] for c in e.value.candidates} == {"Bicicleta roja", "Bicicleta azul"}


def test_selection_of_unavailable_item(db, make_user, publish_item):
    owner, buyer = make_user(), make_user()
    item = publish_item(owner)
    from app.services.publications import service as pubs

    pubs.delete_publication(db, owner, item.id)
    db.commit()
    with pytest.raises(InvalidState):
        search.resolve_selection(db, buyer, item.public_id, search_session_id=None)


def test_impressions_counted(db, make_user, catalog):
    buyer = make_user()
    search.start_search(db, buyer, "auriculares")
    db.commit()
    assert sum(i.search_impressions for i in db.execute(select(Item)).scalars()) == 5
