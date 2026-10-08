"""Public identifiers and security keywords.

Public codes (ITEM-8F42K, OFR-..., OP-...) use a Crockford-like alphabet without
ambiguous characters (no 0/O, 1/I/L, U) and are generated with ``secrets``.
"""

from __future__ import annotations

import secrets

PUBLIC_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 5

# Neutral Spanish words (not derived from any personal data).
KEYWORD_WORDS = (
    "LUNA",
    "SOL",
    "RIO",
    "MAR",
    "NUBE",
    "ROCA",
    "PINO",
    "FARO",
    "LAGO",
    "MONTE",
    "TRIGO",
    "CEDRO",
    "OLIVO",
    "PUMA",
    "COBRE",
    "PLATA",
    "BRISA",
    "DUNA",
    "CIELO",
    "PAMPA",
    "VIENTO",
    "TRUENO",
    "ARENA",
    "CORAL",
    "ROBLE",
    "SAUCE",
    "CONDOR",
    "HALCON",
    "TIGRE",
    "ZORRO",
    "LIMA",
    "MENTA",
    "CANELA",
    "AMBAR",
    "JADE",
    "ONIX",
    "PERLA",
    "RUBI",
    "TOPACIO",
    "COMETA",
    "ORION",
    "VEGA",
    "LIRA",
    "DELTA",
    "BAHIA",
    "COSTA",
    "SIERRA",
    "VALLE",
    "PRADO",
    "ISLA",
)


def new_public_code(prefix: str, length: int = CODE_LENGTH) -> str:
    return f"{prefix}-" + "".join(secrets.choice(PUBLIC_ALPHABET) for _ in range(length))


def new_item_code() -> str:
    return new_public_code("ITEM")


def new_offer_code() -> str:
    return new_public_code("OFR")


def new_transaction_code() -> str:
    return new_public_code("OP")


def new_security_keyword() -> str:
    """Random keyword like ``LUNA-7428`` (≈ 50 * 10^4 = 500k combinations).

    Uniqueness per operation is enforced by a UNIQUE constraint; callers retry on collision.
    """
    return f"{secrets.choice(KEYWORD_WORDS)}-{secrets.randbelow(10_000):04d}"
