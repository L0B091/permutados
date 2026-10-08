"""Search with stable pagination: 5 per page, never repeats an item within a search session."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, not_, select, update
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import Ambiguous, InvalidState, NotFound, ValidationFailed
from app.core.text import normalize
from app.models.conversation import SearchSession
from app.models.enums import InteractionKind, ItemStatus, UserStatus
from app.models.item import Item
from app.models.user import User, Zone
from app.services.channel import interactions
from app.services.publications.service import get_by_public_id, public_view
from app.services.search.query_parser import SearchFilters, parse_query

PAGE_SIZE = 5  # hard rule (principle 9): never more than 5 per page

_ORDINALS = {
    "primero": 1,
    "primer": 1,
    "primera": 1,
    "segundo": 2,
    "segunda": 2,
    "tercero": 3,
    "tercer": 3,
    "tercera": 3,
    "cuarto": 4,
    "cuarta": 4,
    "quinto": 5,
    "quinta": 5,
    "ultimo": -1,
    "ultima": -1,
}


@dataclass
class SearchPage:
    search_session_id: int
    page: int
    results: list[dict[str, Any]]
    exhausted: bool
    used_fallback: bool = False


def _zones(session: Session) -> list[tuple[str, str]]:
    return [(z.slug, normalize(z.name)) for z in session.execute(select(Zone).where(Zone.active.is_(True))).scalars()]


def _tsquery(terms: list[str], op: str) -> str:
    safe = [t for t in (normalize(t) for t in terms) if t.isalnum()]
    return f" {op} ".join(f"{t}:*" for t in safe)


def _base_query(session: Session, user: User, filters: SearchFilters, shown: list[int], *, any_term: bool):
    now = utcnow()
    stmt = select(Item).where(
        Item.status == ItemStatus.PUBLISHED,
        Item.expires_at > now,
        Item.owner_id != user.id,
        # Items of suspended/banned owners are never shown.
        Item.owner_id.in_(select(User.id).where(User.status == UserStatus.ACTIVE)),
    )
    if shown:
        stmt = stmt.where(not_(Item.id.in_(shown)))
    rank = None
    if filters.terms:
        q = _tsquery(filters.terms, "|" if any_term else "&")
        if q:
            tsq = func.to_tsquery("spanish", q)
            stmt = stmt.where(Item.search_vector.op("@@")(tsq))
            rank = func.ts_rank(Item.search_vector, tsq)
    if filters.zone_slug:
        stmt = stmt.join(Zone, Zone.id == Item.zone_id).where(Zone.slug == filters.zone_slug)
    if filters.max_price:
        stmt = stmt.where(Item.base_price <= Decimal(filters.max_price))
    if filters.accepts_swap:
        stmt = stmt.where(Item.accepts_swap.is_(True))
    if filters.category:
        stmt = stmt.where(Item.category == normalize(filters.category))
    order = [rank.desc()] if rank is not None else []
    order += [Item.featured.desc(), Item.published_at.desc(), Item.id.desc()]
    return stmt.order_by(*order)


def _fetch_page(session: Session, user: User, ss: SearchSession) -> SearchPage:
    filters = SearchFilters(**{k: v for k, v in ss.filters.items() if k in SearchFilters.__dataclass_fields__})
    any_term = bool(ss.filters.get("_fallback_any_term"))
    items = list(
        session.execute(
            _base_query(session, user, filters, list(ss.shown_item_ids), any_term=any_term).limit(PAGE_SIZE)
        ).scalars()
    )
    used_fallback = False
    if not items and ss.page == 0 and filters.terms and len(filters.terms) > 1 and not any_term:
        # No item matches all words: broaden to any word (recorded so later pages are consistent).
        ss.filters = {**ss.filters, "_fallback_any_term": True}
        items = list(
            session.execute(
                _base_query(session, user, filters, list(ss.shown_item_ids), any_term=True).limit(PAGE_SIZE)
            ).scalars()
        )
        used_fallback = True
    ids = [i.id for i in items]
    ss.shown_item_ids = list(ss.shown_item_ids) + ids
    ss.last_page_item_ids = ids if ids else list(ss.last_page_item_ids)
    ss.page += 1 if ids else 0
    ss.exhausted = len(items) < PAGE_SIZE
    if ids:
        session.execute(update(Item).where(Item.id.in_(ids)).values(search_impressions=Item.search_impressions + 1))
        interactions.record(session, user.id, ids, InteractionKind.IMPRESSION)
    session.flush()
    results = [{"n": n, **public_view(session, it)} for n, it in enumerate(items, start=1)]
    return SearchPage(ss.id, ss.page, results, ss.exhausted, used_fallback)


def start_search(session: Session, user: User, query: str, *, overrides: dict[str, Any] | None = None) -> SearchPage:
    """``overrides``: structured filters proposed by Dolphin; validated here, never trusted blindly."""
    query = (query or "").strip()[:200]
    if not query:
        raise ValidationFailed("¿Qué estás buscando?", code="EMPTY_QUERY")
    filters = parse_query(query, _zones(session))
    for key in ("zone_slug", "max_price", "accepts_swap", "category"):
        if overrides and overrides.get(key) is not None:
            setattr(filters, key, overrides[key])
    if filters.max_price is not None:
        try:
            if Decimal(str(filters.max_price)) <= 0:
                raise ValueError
            filters.max_price = str(Decimal(str(filters.max_price)))
        except Exception as e:
            raise ValidationFailed("precio máximo inválido", code="INVALID_PRICE") from e
    if filters.zone_slug and filters.zone_slug not in {s for s, _ in _zones(session)}:
        filters.zone_slug = None
    ss = SearchSession(
        user_id=user.id, query=query, filters=filters.to_dict(), shown_item_ids=[], last_page_item_ids=[]
    )
    session.add(ss)
    session.flush()
    return _fetch_page(session, user, ss)


def next_page(session: Session, user: User, search_session_id: int) -> SearchPage:
    ss = session.execute(
        select(SearchSession)
        .where(SearchSession.id == search_session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if ss is None or ss.user_id != user.id:
        raise NotFound("No hay una búsqueda activa.", code="SEARCH_NOT_FOUND")
    if ss.exhausted:
        return SearchPage(ss.id, ss.page, [], True)
    return _fetch_page(session, user, ss)


def resolve_selection(session: Session, user: User, text: str, *, search_session_id: int | None) -> Item:
    """Map "el 2", "el segundo", "ITEM-8F42K" or "la bici roja" to exactly one item. Ask if ambiguous."""
    raw = (text or "").strip()
    code = raw.upper()
    import re

    m = re.search(r"ITEM-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5,8}", code)
    if m:
        item = get_by_public_id(session, m.group(0))
        return _check_selectable(session, user, item)
    ss = session.get(SearchSession, search_session_id) if search_session_id else None
    if ss is None or ss.user_id != user.id or not ss.last_page_item_ids:
        raise NotFound(
            "Decime el código del artículo (por ejemplo ITEM-8F42K) o hacé una búsqueda.", code="NO_RECENT_RESULTS"
        )
    page_ids = list(ss.last_page_item_ids)
    norm = normalize(raw)
    num = re.fullmatch(r"(?:el|la|numero|nro|n|#)?\s*(\d{1,2})", norm)
    idx = None
    if num:
        idx = int(num.group(1))
    else:
        for word, val in _ORDINALS.items():
            if re.search(rf"\b{word}\b", norm):
                idx = val
                break
    if idx is not None:
        if idx == -1:
            idx = len(page_ids)
        if not 1 <= idx <= len(page_ids):
            raise ValidationFailed(f"Elegí un número entre 1 y {len(page_ids)}.", code="SELECTION_OUT_OF_RANGE")
        item = session.get(Item, page_ids[idx - 1])
        assert item is not None
        return _check_selectable(session, user, item)
    tokens = [
        t
        for t in norm.split()
        if len(t) > 2 and t not in {"el", "la", "los", "las", "quiero", "ese", "esa", "este", "esta", "del", "que"}
    ]
    items = [session.get(Item, i) for i in page_ids]
    scored = []
    for it in items:
        if it is None:
            continue
        score = sum(1 for t in tokens if t in (it.search_text or "") or t.rstrip("s") in (it.search_text or ""))
        if score:
            scored.append((score, it))
    if not scored:
        raise NotFound(
            "No identifiqué cuál artículo querés. Decime el número (1 a 5) o el código.", code="SELECTION_NOT_FOUND"
        )
    best = max(s for s, _ in scored)
    top = [it for s, it in scored if s == best]
    if len(top) > 1:
        raise Ambiguous(
            "¿Cuál de estos querés?",
            [{"n": page_ids.index(it.id) + 1, "item_id": it.public_id, "title": it.title} for it in top],
        )
    return _check_selectable(session, user, top[0])


def _check_selectable(session: Session, user: User, item: Item) -> Item:
    if item.owner_id == user.id:
        raise InvalidState("Ese artículo es tuyo.", code="OWN_ITEM")
    if item.status != ItemStatus.PUBLISHED or (item.expires_at and item.expires_at <= utcnow()):
        raise InvalidState("Ese artículo ya no está disponible.", code="ITEM_NOT_AVAILABLE")
    session.execute(update(Item).where(Item.id == item.id).values(selection_count=Item.selection_count + 1))
    interactions.record(session, user.id, [item.id], InteractionKind.SELECTION)
    return item


__all__ = ["PAGE_SIZE", "SearchPage", "next_page", "resolve_selection", "start_search"]
