"""Live business configuration stored in ``system_config`` (editable from the admin console)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import SystemModeBlocked, ValidationFailed
from app.models.enums import SystemMode
from app.models.system import SystemConfig


def _int_range(lo: int, hi: int) -> Callable[[Any], int]:
    def v(x: Any) -> int:
        if isinstance(x, bool):
            raise ValueError("not an integer")
        x = int(x)
        if not lo <= x <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return x

    return v


def _price(x: Any) -> str:
    try:
        d = Decimal(str(x))
    except InvalidOperation as e:
        raise ValueError("invalid amount") from e
    if d <= 0 or d > Decimal("10000000"):
        raise ValueError("amount out of range")
    return str(d.quantize(Decimal("0.01")))


def _currency(x: Any) -> str:
    x = str(x).upper()
    if x not in {"ARS"}:
        raise ValueError("only ARS is supported by the configured Mercado Pago account")
    return x


def _bool(x: Any) -> bool:
    if not isinstance(x, bool):
        raise ValueError("must be a boolean")
    return x


def _mode(x: Any) -> str:
    return SystemMode(str(x)).value


VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "contact_release_price": _price,
    "contact_release_currency": _currency,
    "offer_expiration_hours": _int_range(1, 168),
    "payment_window_hours": _int_range(1, 168),
    "publication_expiration_days": _int_range(1, 365),
    "publication_expiry_warning_days": _int_range(0, 30),
    "max_active_offers_per_bidder": _int_range(1, 50),
    "system_mode": _mode,
    "channel_enabled": _bool,
    "channel_daily_limit": _int_range(0, 200),
    "channel_min_score": _int_range(0, 100),
    "channel_max_exposure_per_item": _int_range(0, 100),
    "channel_min_hours_between_same_item": _int_range(0, 24 * 60),
    # Manual channel (sections 26-29, 33): ranking/diversity/exposure knobs for the suggestions list.
    "channel_category_diversity": _bool,
    "channel_zone_diversity": _bool,
    "channel_price_diversity": _bool,
    "channel_featured_max_per_batch": _int_range(0, 50),
    "channel_min_minutes_between_posts": _int_range(0, 24 * 60),
    "channel_exposure_decay_hours": _int_range(1, 24 * 30),
    "channel_refresh_hours": _int_range(1, 24 * 30),
    "channel_removal_overdue_hours": _int_range(1, 24 * 7),
}

# Internal state stored in system_config but never editable through the admin config endpoint.
INTERNAL_KEYS = ("system_paused_since",)


def defaults() -> dict[str, Any]:
    s = get_settings()
    return {
        "contact_release_price": _price(s.contact_release_price),
        "contact_release_currency": _currency(s.contact_release_currency),
        "offer_expiration_hours": s.offer_expiration_hours,
        "payment_window_hours": s.payment_window_hours,
        "publication_expiration_days": s.publication_expiration_days,
        "publication_expiry_warning_days": s.publication_expiry_warning_days,
        "max_active_offers_per_bidder": s.max_active_offers_per_bidder,
        "system_mode": SystemMode.ONLINE.value,
        "channel_enabled": s.channel_enabled,
        "channel_daily_limit": s.channel_daily_limit,
        "channel_min_score": s.channel_min_score,
        "channel_max_exposure_per_item": s.channel_max_exposure_per_item,
        "channel_min_hours_between_same_item": s.channel_min_hours_between_same_item,
        "channel_category_diversity": True,
        "channel_zone_diversity": True,
        "channel_price_diversity": True,
        "channel_featured_max_per_batch": 3,
        "channel_min_minutes_between_posts": 30,
        "channel_exposure_decay_hours": 72,
        "channel_refresh_hours": 72,
        "channel_removal_overdue_hours": 6,
    }


def ensure_defaults(session: Session) -> None:
    """Insert missing keys (never overwrites values changed by an admin)."""
    for key, value in defaults().items():
        session.execute(
            insert(SystemConfig).values(key=key, value=value).on_conflict_do_nothing(index_elements=["key"])
        )
    session.flush()


def get_value(session: Session, key: str) -> Any:
    if key not in VALIDATORS:
        raise KeyError(key)
    row = session.execute(select(SystemConfig.value).where(SystemConfig.key == key)).scalar_one_or_none()
    return row if row is not None else defaults()[key]


def get_all(session: Session) -> dict[str, Any]:
    out = defaults()
    for row in session.execute(select(SystemConfig)).scalars():
        if row.key in VALIDATORS:
            out[row.key] = row.value
    return out


def set_value(session: Session, key: str, value: Any, *, admin_id: int | None = None) -> Any:
    if key not in VALIDATORS:
        raise ValidationFailed(f"unknown config key {key!r}", code="UNKNOWN_CONFIG_KEY")
    try:
        clean = VALIDATORS[key](value)
    except (ValueError, TypeError) as e:
        raise ValidationFailed(f"invalid value for {key}: {e}", code="INVALID_CONFIG_VALUE") from e
    if key == "system_mode":
        from app.services.system import modes

        before = current_mode(session)
        _write(session, key, clean, admin_id)
        modes.on_mode_change(session, before, SystemMode(clean), admin_id=admin_id)
        return clean
    _write(session, key, clean, admin_id)
    return clean


def _write(session: Session, key: str, clean: Any, admin_id: int | None) -> None:
    stmt = insert(SystemConfig).values(key=key, value=clean, updated_by_admin_id=admin_id)
    stmt = stmt.on_conflict_do_update(index_elements=["key"], set_={"value": clean, "updated_by_admin_id": admin_id})
    session.execute(stmt)
    session.flush()


def get_internal(session: Session, key: str) -> Any:
    assert key in INTERNAL_KEYS
    return session.execute(select(SystemConfig.value).where(SystemConfig.key == key)).scalar_one_or_none()


def set_internal(session: Session, key: str, value: Any) -> None:
    assert key in INTERNAL_KEYS
    if value is None:
        session.query(SystemConfig).filter(SystemConfig.key == key).delete()
        session.flush()
        return
    _write(session, key, value, None)


# ---- convenience typed getters ---------------------------------------------------------------
def release_price(session: Session) -> tuple[Decimal, str]:
    return Decimal(str(get_value(session, "contact_release_price"))), str(
        get_value(session, "contact_release_currency")
    )


def offer_hours(session: Session) -> int:
    return int(get_value(session, "offer_expiration_hours"))


def payment_hours(session: Session) -> int:
    return int(get_value(session, "payment_window_hours"))


def publication_days(session: Session) -> int:
    return int(get_value(session, "publication_expiration_days"))


# ---- system mode (section 46) -------------------------------------------------------------------
class CriticalOp:
    PUBLISH = "PUBLISH"
    EDIT = "EDIT"
    OFFER = "OFFER"
    ACCEPT = "ACCEPT"
    PAYMENT_CREATE = "PAYMENT_CREATE"
    CONTACT_RELEASE = "CONTACT_RELEASE"


_BLOCKED = {
    SystemMode.ONLINE: set(),
    # PAUSED: no new critical operations, no new releases. Publishing/search keep working.
    SystemMode.PAUSED: {CriticalOp.OFFER, CriticalOp.ACCEPT, CriticalOp.PAYMENT_CREATE, CriticalOp.CONTACT_RELEASE},
    # EMERGENCY: every critical / state-changing user operation is blocked.
    SystemMode.EMERGENCY: {
        CriticalOp.PUBLISH,
        CriticalOp.EDIT,
        CriticalOp.OFFER,
        CriticalOp.ACCEPT,
        CriticalOp.PAYMENT_CREATE,
        CriticalOp.CONTACT_RELEASE,
    },
}


def current_mode(session: Session) -> SystemMode:
    return SystemMode(get_value(session, "system_mode"))


def is_allowed(session: Session, op: str) -> bool:
    return op not in _BLOCKED[current_mode(session)]


def require_allowed(session: Session, op: str) -> None:
    mode = current_mode(session)
    if op in _BLOCKED[mode]:
        raise SystemModeBlocked(
            "Permutados está temporalmente en mantenimiento. Probá de nuevo en un rato.",
            details={"mode": mode.value, "operation": op},
        )
