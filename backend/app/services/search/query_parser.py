"""Deterministic natural-language search parsing ("busco auriculares en palermo hasta 50 mil")."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from app.core.text import normalize

STOPWORDS = {
    "busco",
    "buscando",
    "quiero",
    "necesito",
    "tenes",
    "tienen",
    "hay",
    "algun",
    "alguna",
    "algo",
    "un",
    "una",
    "unos",
    "unas",
    "el",
    "la",
    "los",
    "las",
    "de",
    "del",
    "en",
    "para",
    "por",
    "con",
    "que",
    "me",
    "y",
    "o",
    "a",
    "vendo",
    "compro",
    "comprar",
    "hola",
    "porfa",
    "por favor",
    "favor",
    "cerca",
    "zona",
    "barato",
    "barata",
    "usado",
    "usada",
    "nuevo",
    "nueva",
    "mi",
    "estoy",
    "ando",
    "se",
    "venda",
    "vende",
    "acepte",
    "acepten",
    "permuta",
    "permutas",
    "cambio",
    "hasta",
    "menos",
    "max",
    "maximo",
    "pesos",
    "mil",
    "k",
    "lucas",
}

NEXT_PAGE_COMMANDS = {
    "mas",
    "siguiente",
    "ver mas",
    "mostrame mas",
    "mas resultados",
    "otros",
    "otra pagina",
    "siguientes",
    "dale mas",
}

_PRICE_MAX = re.compile(r"(?:hasta|menos de|max(?:imo)?|no mas de|tope)\s*\$?\s*([\d.,]+)\s*(mil|k|lucas)?")
_SWAP = re.compile(r"\b(?:acepte[n]?|acepta[n]?|con|que haga[n]?)\s+(?:permuta|permutas|cambio|trueque)\b")


@dataclass
class SearchFilters:
    terms: list[str] = field(default_factory=list)
    zone_slug: str | None = None
    max_price: str | None = None
    accepts_swap: bool | None = None
    category: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def is_next_page_command(text: str) -> bool:
    t = normalize(text).strip(" .!?")
    return t in NEXT_PAGE_COMMANDS


def _parse_amount(num: str, unit: str | None) -> Decimal | None:
    s = num.replace(".", "").replace(",", ".")
    try:
        v = Decimal(s)
    except Exception:
        return None
    if unit in {"mil", "k", "lucas"}:
        v *= 1000
    return v if v > 0 else None


def parse_query(text: str, zones: list[tuple[str, str]] | None = None) -> SearchFilters:
    """``zones``: list of (slug, normalised name) known to the backend."""
    q = normalize(text)
    f = SearchFilters()
    m = _PRICE_MAX.search(q)
    if m:
        amount = _parse_amount(m.group(1), m.group(2))
        if amount is not None:
            f.max_price = str(amount)
        q = q[: m.start()] + " " + q[m.end() :]
    if _SWAP.search(q):
        f.accepts_swap = True
        q = _SWAP.sub(" ", q)
    for slug, name in sorted(zones or [], key=lambda z: -len(z[1])):
        pat = re.compile(rf"\b(?:en\s+)?{re.escape(name)}\b")
        if pat.search(q):
            f.zone_slug = slug
            q = pat.sub(" ", q)
            break
    terms = []
    for tok in re.findall(r"[a-z0-9]+", q):
        if tok in STOPWORDS or len(tok) < 2:
            continue
        if tok not in terms:
            terms.append(tok)
    f.terms = terms[:8]
    return f
