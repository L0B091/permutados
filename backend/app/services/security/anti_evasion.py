"""Deterministic detector of attempts to exchange contact data outside Permutados (section 38).

Pure function, no AI involved. Tuned to avoid false positives on prices ("$150.000",
"1.500.000 pesos", "150 mil"), models ("iPhone 13 128gb"), sizes and dimensions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.text import normalize, strip_accents


@dataclass(frozen=True)
class Detection:
    kind: str
    evidence: str


@dataclass(frozen=True)
class EvasionResult:
    detections: list[Detection] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        return bool(self.detections)

    @property
    def kinds(self) -> list[str]:
        return sorted({d.kind for d in self.detections})


_EMAIL = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}")
_EMAIL_OBFUSCATED = re.compile(
    r"\b[a-z0-9._\-]{2,}\s*(?:\(|\[)?\s*(?:arroba|at)\s*(?:\)|\])?\s*[a-z0-9\-]{2,}\s*"
    r"(?:\(|\[)?\s*(?:punto|dot|\.)\s*(?:\)|\])?\s*(?:com|net|org|ar|io|es)\b"
)
_URL = re.compile(
    r"(?:https?://|www\.)\S+|\b[a-z0-9\-]{2,}\.(?:com|net|org|io|me|ly|app|link|site|xyz|info|com\.ar|ar)(?:/\S*)?\b"
)
_SOCIAL = {
    "TELEGRAM": re.compile(r"\b(?:telegram|telegran|t\.me|tg\s*:)\b|\bt\.me/"),
    "INSTAGRAM": re.compile(r"\b(?:instagram|insta|instag|ig\s*:)\b|\bmi\s+ig\b|\bpor\s+ig\b"),
    "FACEBOOK": re.compile(r"\b(?:facebook|face\s*book|fb|messenger|fb\.com|m\.me)\b"),
    "OTHER_NETWORK": re.compile(
        r"\b(?:tiktok|twitter|snapchat|snap|signal|discord|linkedin|threads|wa\.me|whatsapp\.com|skype|line\s*id)\b"
    ),
}
_HANDLE = re.compile(r"(?<![\w.])@[a-z0-9_.]{3,30}\b")
_EVASION_PHRASES = re.compile(
    r"\b(?:"
    r"pasame\s+(?:tu|el)\s+(?:numero|cel|celular|telefono|whats?app|wpp|wsp|contacto|mail|email|correo)"
    r"|te\s+paso\s+mi\s+(?:numero|cel|celular|telefono|whats?app|wpp|wsp|contacto|mail|email|correo)"
    r"|mi\s+(?:numero|cel|celular|telefono|whats?app|wpp|wsp|mail|email|correo)\s+es"
    r"|(?:escribime|llamame|hablame|mandame\s+(?:un\s+)?(?:mensaje|msj|wpp|wsp))\s+(?:al|a\s+mi|por)"
    r"|hablemos\s+(?:por\s+)?(?:fuera|afuera|privado|por\s+otro\s+lado|directo)"
    r"|(?:arreglamos|arreglemos|coordinamos|coordinemos|cerramos|cerremos)\s+(?:por\s+)?(?:fuera|afuera|privado|directo)"
    r"|(?:sin|evitar|ahorrar(?:nos)?|saltear|esquivar)\s+(?:pagar\s+)?(?:la\s+)?(?:comision|tarifa|fee|permutados)"
    r"|por\s+fuera\s+de\s+(?:la\s+app|permutados|aca)"
    r"|fuera\s+de\s+(?:la\s+app|permutados)"
    r"|agregame\s+(?:a|al|en)"
    r"|(?:mandame|enviame|escribime|hablame)\s+(?:un\s+|al\s+|por\s+)?(?:wpp|wsp|whats?app|wasap|guasap|mail|email|correo|sms)"
    r"|buscame\s+(?:en|como)"
    r")\b"
)
_NUMBER_WORDS = ("cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve")
_SPELLED_DIGITS = re.compile(
    r"\b(?:(?:" + "|".join(_NUMBER_WORDS) + r")[\s,.\-]+){7,}(?:" + "|".join(_NUMBER_WORDS) + r")\b"
)

# Digit runs with phone-like separators (spaces, dashes, dots, parentheses, +).
_DIGIT_RUN = re.compile(r"\+?\(?\d[\d\s\-.()]{6,}\d")
_PRICE_GROUPED = re.compile(r"^\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?$")
_CURRENCY_BEFORE = re.compile(r"(?:\$|ars|usd|u\$s|us\$|precio|pesos|vale|valor|oferta|ofrezco|doy|pago)\s*:?\s*$")
_CURRENCY_AFTER = re.compile(r"^\s*(?:pesos|ars|usd|dolares|mil|k\b|lucas)")


def _is_price_context(text: str, start: int, end: int, candidate: str) -> bool:
    before = text[max(0, start - 12) : start]
    after = text[end : end + 10]
    stripped = candidate.strip()
    if _PRICE_GROUPED.match(stripped):
        return True
    if _CURRENCY_BEFORE.search(before) or _CURRENCY_AFTER.match(after):
        return True
    return False


def _phone_candidates(text: str) -> list[str]:
    found = []
    for m in _DIGIT_RUN.finditer(text):
        cand = m.group(0)
        digits = re.sub(r"\D", "", cand)
        if not 8 <= len(digits) <= 15:
            continue
        if _is_price_context(text, m.start(), m.end(), cand):
            continue
        # Several short, space-separated numbers like "2019 2020 2021" or "13 128 256" are not phones.
        tokens = [t for t in re.split(r"[\s]+", cand.strip()) if t]
        if (
            len(tokens) >= 3
            and all(len(re.sub(r"\D", "", t)) <= 4 for t in tokens)
            and all(re.fullmatch(r"(?:19|20)\d\d", t) or len(t) <= 3 for t in tokens)
        ):
            continue
        found.append(cand.strip())
    return found


def detect(text: str | None) -> EvasionResult:
    if not text:
        return EvasionResult()
    raw = strip_accents(text.lower())
    norm = normalize(text)
    detections: list[Detection] = []

    for m in _EMAIL.finditer(raw):
        detections.append(Detection("EMAIL", m.group(0)))
    if not any(d.kind == "EMAIL" for d in detections):
        m = _EMAIL_OBFUSCATED.search(norm)
        if m:
            detections.append(Detection("EMAIL", m.group(0)))
    for kind, rx in _SOCIAL.items():
        m = rx.search(norm)
        if m:
            detections.append(Detection(kind, m.group(0)))
    emails = " ".join(d.evidence for d in detections if d.kind == "EMAIL")
    for m in _URL.finditer(raw):
        if m.group(0) not in emails and not any(m.group(0) in e for e in emails.split()):
            detections.append(Detection("URL", m.group(0)))
    for m in _HANDLE.finditer(raw):
        if not any(m.group(0)[1:] in d.evidence for d in detections if d.kind == "EMAIL"):
            detections.append(Detection("SOCIAL_HANDLE", m.group(0)))
    for cand in _phone_candidates(raw):
        detections.append(Detection("PHONE", cand))
    m = _SPELLED_DIGITS.search(norm)
    if m:
        detections.append(Detection("PHONE_SPELLED", m.group(0)))
    m = _EVASION_PHRASES.search(norm)
    if m:
        detections.append(Detection("EVASION_PHRASE", m.group(0)))
    return EvasionResult(detections)
