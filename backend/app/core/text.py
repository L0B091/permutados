from __future__ import annotations

import re
import unicodedata


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize(s: str) -> str:
    """Lower-case, accent-free, single-spaced."""
    s = strip_accents((s or "").lower())
    s = re.sub(r"[^\w\s@.:/+\-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_user_text(s: str | None, max_len: int) -> str | None:
    """Sanitise free text from users: strip control chars, collapse whitespace, cap length."""
    if s is None:
        return None
    s = "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s[:max_len] or None
