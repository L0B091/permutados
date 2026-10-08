"""Deterministic intent detection (backend side).

The detected intent is a *hint* given to Dolphin in BACKEND_CONTEXT and stored in the conversation; it never
executes anything by itself. It also lets the backend answer unambiguous commands without the model ("más") and
keeps the state machine honest when the model is unavailable.
"""

from __future__ import annotations

import re
from enum import StrEnum

from app.core.text import normalize
from app.services.search.query_parser import is_next_page_command


class Intent(StrEnum):
    PUBLISH = "PUBLISH"
    SEARCH = "SEARCH"
    NEXT_PAGE = "NEXT_PAGE"
    SELECT = "SELECT"
    OFFER = "OFFER"
    ACCEPT_OFFER = "ACCEPT_OFFER"
    REJECT_OFFER = "REJECT_OFFER"
    CANCEL = "CANCEL"
    PAY = "PAY"
    PAYMENT_STATUS = "PAYMENT_STATUS"
    MY_PUBLICATIONS = "MY_PUBLICATIONS"
    EDIT_PUBLICATION = "EDIT_PUBLICATION"
    DELETE_PUBLICATION = "DELETE_PUBLICATION"
    REPORT = "REPORT"
    CONFIRM = "CONFIRM"
    DENY = "DENY"
    GREETING = "GREETING"
    HELP = "HELP"
    UNKNOWN = "UNKNOWN"


def _rx(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns))


_CONFIRM = _rx(
    r"^(si|sii+|sip|dale|ok|okay|oka|confirmo|correcto|exacto|perfecto|de una|listo|joya|claro|obvio|"
    r"publicala|publicalo|publicar|esta bien|todo bien|va|vamos)( (dale|si|gracias|publicala|publicalo))*$"
)
_DENY = _rx(r"^(no|nop|nope|para|nada|todavia no|no gracias|negativo)( gracias)?$")
_REJECT = _rx(r"\brechaz\w*", r"\bno acepto\b", r"\bno me interesa la oferta\b")
_ACCEPT = _rx(r"\bacept\w*")
_CANCEL = _rx(r"\bcancel\w*", r"\bme arrepiento\b", r"\bme arrepenti\b", r"\bdar de baja la oferta\b")
_PAYMENT_STATUS = _rx(r"\bya (te )?pague\b", r"\bpague\b", r"\bestado del pago\b", r"\bse acredito\b")
_PAY = _rx(r"\bpagar\b", r"\blink de pago\b", r"\bquiero pagar\b", r"\bcomo pago\b", r"\bpago\b")
_OFFER = _rx(
    r"\bofrezco\b",
    r"\boferto\b",
    r"\bofertar\b",
    r"\bte doy\b",
    r"\bhago una oferta\b",
    r"\bmi oferta\b",
    r"\bpermuto\b",
    r"\bcambio por\b",
    r"\bte lo cambio\b",
)
_DELETE = _rx(r"\b(borr|elimin|baj)\w* (la |mi )?publicacion\b", r"\bya (lo|la) vendi\b")
_EDIT = _rx(r"\b(cambi|modific|edit|actualiz)\w* (el |la |mi )?(precio|publicacion|zona|descripcion)\b")
_MINE = _rx(r"\bmis publicaciones\b", r"\bque tengo publicado\b", r"\bmis articulos\b")
_REPORT = _rx(r"\b(denunci|report)\w*")
_SEARCH = _rx(
    r"\bbusco\b",
    r"\bbuscar\b",
    r"\bbuscando\b",
    r"\bquiero comprar\b",
    r"\btenes\b",
    r"\bhay\b",
    r"\bnecesito\b",
    r"\bme interesa (un|una)\b",
)
_PUBLISH = _rx(r"\bpublicar\b", r"\bvender\b", r"\bvendo\b", r"\bquiero publicar\b", r"\bpublico\b")
_SELECT = _rx(
    r"^item-[0-9a-z]{5,8}$",
    r"\bitem-[0-9a-z]{5,8}\b",
    r"^(el |la )?[1-5]$",
    r"^(el |la )?(primer[oa]?|segund[oa]|tercer[oa]?|cuart[oa]|quint[oa])$",
)
_GREETING = _rx(r"^(hola|buenas|buen dia|buenos dias|buenas tardes|buenas noches|hey|que tal)\b")
_HELP = _rx(r"\bayuda\b", r"\bcomo funciona\b", r"\bque podes hacer\b", r"\bmenu\b")


def detect_intent(text: str | None, *, has_image: bool = False, state: str | None = None) -> Intent:
    if has_image:
        return Intent.PUBLISH
    t = normalize(text or "")
    if not t:
        return Intent.UNKNOWN
    if is_next_page_command(t):
        return Intent.NEXT_PAGE
    if _CONFIRM.search(t) and state and state != "IDLE":
        return Intent.CONFIRM
    if _DENY.search(t) and state and state != "IDLE":
        return Intent.DENY
    for rx, intent in (
        (_DELETE, Intent.DELETE_PUBLICATION),
        (_EDIT, Intent.EDIT_PUBLICATION),
        (_MINE, Intent.MY_PUBLICATIONS),
        (_REJECT, Intent.REJECT_OFFER),
        (_CANCEL, Intent.CANCEL),
        (_ACCEPT, Intent.ACCEPT_OFFER),
        (_PAYMENT_STATUS, Intent.PAYMENT_STATUS),
        (_PAY, Intent.PAY),
        (_OFFER, Intent.OFFER),
        (_REPORT, Intent.REPORT),
    ):
        if rx.search(t):
            return intent
    if _SELECT.search(t) and state in ("BROWSING_RESULTS", "ITEM_SELECTED", None) or re.search(r"\bitem-", t):
        return Intent.SELECT
    if _SEARCH.search(t):
        return Intent.SEARCH
    if _PUBLISH.search(t):
        return Intent.PUBLISH
    if _HELP.search(t):
        return Intent.HELP
    if _GREETING.search(t):
        return Intent.GREETING
    return Intent.UNKNOWN
