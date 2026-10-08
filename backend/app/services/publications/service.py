"""Publications: draft -> identification -> confirmation -> preview -> PUBLISHED, edit, delete.

All lookups are scoped by ``owner_id``; the AI never decides ownership or prices.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import Ambiguous, DomainError, Forbidden, InvalidState, NotFound, ValidationFailed
from app.core.ids import new_item_code
from app.core.text import clean_user_text, normalize
from app.models.enums import ActorType, ItemStatus, OfferCloseReason
from app.models.item import Item, ItemImage
from app.models.user import User, Zone
from app.services.audit.service import AuditAction, audit
from app.services.events.bus import EventType, get_bus
from app.services.notifications import templates
from app.services.publications.image_validation import ValidatedImage, validate_images
from app.services.publications.state import transition_item
from app.services.publications.storage import ImageStorage, LocalImageStorage
from app.services.security.enforcement import EvasionBlocked, enforce_or_raise  # noqa: F401
from app.services.system import config_service as cfg
from app.services.users.service import ensure_active

ACTIVE_OWNER_STATUSES = (ItemStatus.DRAFT, ItemStatus.PUBLISHED, ItemStatus.RESERVED)
EDITABLE_FIELDS = {
    "base_price",
    "zone",
    "accepts_swap",
    "has_damage",
    "damage_description",
    "works",
    "malfunction_description",
    "description",
}
RADICAL_FIELDS = {"title", "category"}
MAX_PRICE = Decimal("1000000000")
AI_IDENTIFICATION_KEYS = {"title", "category", "brand", "model", "color", "confidence", "notes"}


class RadicalChange(DomainError):
    code = "ITEM_CHANGE_REQUIRES_NEW_PUBLICATION"
    http_status = 409


# ---- helpers --------------------------------------------------------------------------------
def parse_price(value: Any) -> Decimal:
    """Parse a user price. Accepts 150000, "150.000", "$150.000", "150000,50". Never invented by AI."""
    if isinstance(value, bool):
        raise ValidationFailed("precio inválido", code="INVALID_PRICE")
    if isinstance(value, int | float | Decimal):
        d = Decimal(str(value))
    else:
        s = str(value).strip().lower().replace("$", "").replace("ars", "").replace(" ", "")
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        elif s.count(".") >= 1 and all(len(p) == 3 for p in s.split(".")[1:]):
            s = s.replace(".", "")
        try:
            d = Decimal(s)
        except InvalidOperation as e:
            raise ValidationFailed("precio inválido", code="INVALID_PRICE") from e
    if d <= 0 or d > MAX_PRICE:
        raise ValidationFailed("precio fuera de rango", code="INVALID_PRICE")
    return d.quantize(Decimal("0.01"))


def resolve_zone(session: Session, value: str) -> Zone:
    """Only known general zones are accepted (never an exact address)."""
    key = normalize(value)
    zones = session.execute(select(Zone).where(Zone.active.is_(True))).scalars().all()
    exact = [z for z in zones if key in (normalize(z.name), normalize(z.slug).replace("-", " "), z.slug)]
    if len(exact) == 1:
        return exact[0]
    partial = [z for z in zones if key and (key in normalize(z.name) or normalize(z.name) in key)]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        raise Ambiguous("¿A qué zona te referís?", [{"slug": z.slug, "name": z.name} for z in partial[:5]])
    raise ValidationFailed(
        "No reconozco esa zona. Indicá tu barrio o localidad (zona general, sin dirección).", code="UNKNOWN_ZONE"
    )


def _refresh_search_text(item: Item, zone: Zone | None = None) -> None:
    ai = item.ai_identification or {}
    parts = [
        item.title or "",
        item.category or "",
        item.description or "",
        ai.get("brand") or "",
        ai.get("model") or "",
    ]
    item.search_text = normalize(" ".join(p for p in parts if p))


def _check_texts(session: Session, user: User, *texts: str | None) -> None:
    enforce_or_raise(session, user.id, *texts, source="publication")


def _get_owned(session: Session, user: User, item_id: int, *, for_update: bool = True) -> Item:
    stmt = select(Item).where(Item.id == item_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    item = session.execute(stmt).scalar_one_or_none()
    if item is None:
        raise NotFound("publicación no encontrada", code="ITEM_NOT_FOUND")
    if item.owner_id != user.id:
        raise Forbidden("no sos el propietario de esta publicación", code="NOT_OWNER")
    return item


def _attach_image(session: Session, item: Item, img: ValidatedImage, storage: ImageStorage) -> None:
    key = storage.save(img.data, img.sha256, img.extension)
    for existing in list(item.images):
        item.images.remove(existing)
    session.flush()
    item.images.append(
        ItemImage(
            position=0,
            storage_key=key,
            mime_type=img.mime_type,
            width=img.width,
            height=img.height,
            size_bytes=img.size_bytes,
            sha256=img.sha256,
            quality_score=img.quality_score,
        )
    )


# ---- creation flow ----------------------------------------------------------------------------
def start_draft(session: Session, user: User, images: list[bytes], *, storage: ImageStorage | None = None) -> Item:
    """Step 1-2: receive exactly one photo, validate it, create a DRAFT. Nothing advances without a valid image."""
    ensure_active(user)
    cfg.require_allowed(session, cfg.CriticalOp.PUBLISH)
    img = validate_images(images)  # raises ImageRejected with the user-facing message
    # Only one open draft per user: older drafts are discarded.
    for old in session.execute(
        select(Item)
        .where(Item.owner_id == user.id, Item.status == ItemStatus.DRAFT)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars():
        transition_item(session, old, ItemStatus.DELETED, actor_type=ActorType.SYSTEM, reason="DRAFT_REPLACED")
    item = Item(
        public_id=_unique_item_code(session),
        owner_id=user.id,
        status=ItemStatus.DRAFT,
        currency=cfg.release_price(session)[1],
    )
    session.add(item)
    session.flush()
    _attach_image(session, item, img, storage or LocalImageStorage())
    session.flush()
    audit(
        session,
        AuditAction.ITEM_DRAFT_CREATED,
        actor_type=ActorType.USER,
        actor_id=user.id,
        entity_type="item",
        entity_id=item.public_id,
    )
    return item


def _unique_item_code(session: Session) -> str:
    for _ in range(10):
        code = new_item_code()
        if not session.execute(select(Item.id).where(Item.public_id == code)).first():
            return code
    raise RuntimeError("could not allocate a unique item code")  # pragma: no cover


def set_ai_identification(session: Session, user: User, item_id: int, suggestion: dict[str, Any]) -> Item:
    """Store the vision model's suggestion. Price-like keys are discarded: the AI never sets prices."""
    item = _get_owned(session, user, item_id)
    if item.status != ItemStatus.DRAFT:
        raise InvalidState("la publicación ya no es un borrador", code="ITEM_NOT_DRAFT")
    clean = {k: clean_user_text(str(v), 200) for k, v in suggestion.items() if k in AI_IDENTIFICATION_KEYS and v}
    item.ai_identification = clean
    if not item.title and clean.get("title"):
        item.title = clean["title"][:120]
    if not item.category and clean.get("category"):
        item.category = normalize(clean["category"])[:60]
    item.identification_confirmed = False
    _refresh_search_text(item)
    session.flush()
    return item


def fill_draft(
    session: Session,
    user: User,
    item_id: int,
    *,
    title: str | None = None,
    category: str | None = None,
    identification_confirmed: bool | None = None,
    zone: str | None = None,
    base_price: Any = None,
    accepts_swap: bool | None = None,
    has_damage: bool | None = None,
    damage_description: str | None = None,
    works: bool | None = None,
    malfunction_description: str | None = None,
    description: str | None = None,
) -> Item:
    """Steps 3-4: user confirms identification and answers only the basic questions."""
    item = _get_owned(session, user, item_id)
    if item.status != ItemStatus.DRAFT:
        raise InvalidState("la publicación ya no es un borrador", code="ITEM_NOT_DRAFT")
    title = clean_user_text(title, 120)
    damage_description = clean_user_text(damage_description, 500)
    malfunction_description = clean_user_text(malfunction_description, 500)
    description = clean_user_text(description, 1000)
    _check_texts(session, user, title, damage_description, malfunction_description, description)
    if title:
        item.title = title
    if category:
        item.category = normalize(category)[:60]
    if identification_confirmed is not None:
        item.identification_confirmed = identification_confirmed
    if zone is not None:
        item.zone_id = resolve_zone(session, zone).id
    if base_price is not None:
        item.base_price = parse_price(base_price)
    if accepts_swap is not None:
        item.accepts_swap = accepts_swap
    if has_damage is not None:
        item.has_damage = has_damage
        if not has_damage:
            item.damage_description = None
    if damage_description:
        item.damage_description = damage_description
    if works is not None:
        item.works = works
        if works:
            item.malfunction_description = None
    if malfunction_description:
        item.malfunction_description = malfunction_description
    if description:
        item.description = description
    _refresh_search_text(item)
    session.flush()
    return item


def missing_fields(item: Item) -> list[str]:
    missing = []
    if not item.images:
        missing.append("image")
    if not item.title:
        missing.append("title")
    if not item.identification_confirmed:
        missing.append("identification_confirmed")
    for f in ("zone_id", "base_price", "accepts_swap", "has_damage", "works"):
        if getattr(item, f) is None:
            missing.append(f.replace("_id", ""))
    if item.has_damage and not item.damage_description:
        missing.append("damage_description")
    if item.works is False and not item.malfunction_description:
        missing.append("malfunction_description")
    return missing


def preview_text(session: Session, item: Item) -> str:
    zone = session.get(Zone, item.zone_id) if item.zone_id else None
    lines = [f"📦 {item.title or '(sin título)'}"]
    if item.description:
        lines.append(item.description)
    lines.append(f"📍 Zona: {zone.name if zone else '-'}")
    lines.append(f"💲 Precio: {templates.money(item.base_price, item.currency) if item.base_price else '-'}")
    lines.append(f"🔁 Acepta permuta: {'Sí' if item.accepts_swap else 'No'}")
    lines.append(f"🩹 Daños: {item.damage_description if item.has_damage else 'No'}")
    lines.append(f"⚙️ Funciona: {'Sí' if item.works else ('No — ' + (item.malfunction_description or ''))}")
    return "\n".join(lines)


def publish(session: Session, user: User, item_id: int) -> Item:
    """Step 6: user confirmed the preview."""
    ensure_active(user)
    cfg.require_allowed(session, cfg.CriticalOp.PUBLISH)
    item = _get_owned(session, user, item_id)
    if item.status != ItemStatus.DRAFT:
        raise InvalidState("la publicación ya fue publicada o cerrada", code="ITEM_NOT_DRAFT")
    missing = missing_fields(item)
    if missing:
        raise ValidationFailed("faltan datos para publicar", code="ITEM_INCOMPLETE", details={"missing": missing})
    now = utcnow()
    item.published_at = now
    item.expires_at = now + timedelta(days=cfg.publication_days(session))
    transition_item(session, item, ItemStatus.PUBLISHED, actor_type=ActorType.USER, actor_id=user.id)
    return item


# ---- owner queries / resolution -----------------------------------------------------------
def list_my_publications(session: Session, user: User, *, include_closed: bool = False) -> list[Item]:
    stmt = select(Item).where(Item.owner_id == user.id)
    if not include_closed:
        stmt = stmt.where(Item.status.in_([ItemStatus.PUBLISHED, ItemStatus.RESERVED]))
    return list(session.execute(stmt.order_by(Item.created_at.desc(), Item.id.desc())).scalars())


def resolve_owned_item(session: Session, user: User, reference: str) -> Item:
    """Resolve "mi bicicleta" / "ITEM-8F42K" among the user's own active publications. Ask if ambiguous."""
    ref = (reference or "").strip()
    items = list_my_publications(session, user)
    if not items:
        raise NotFound("No tenés publicaciones activas.", code="NO_PUBLICATIONS")
    code = ref.upper()
    for it in items:
        if it.public_id == code:
            return it
    tokens = [t for t in normalize(ref).split() if len(t) > 2 and t not in _STOP]
    if not tokens:
        if len(items) == 1:
            return items[0]
        raise Ambiguous("¿Cuál de tus publicaciones?", [_candidate(i) for i in items])
    scored = []
    for it in items:
        hay = it.search_text or normalize(it.title or "")
        score = sum(1 for t in tokens if t in hay or t.rstrip("s") in hay)
        if score:
            scored.append((score, it))
    if not scored:
        raise NotFound("No encontré esa publicación entre las tuyas.", code="ITEM_NOT_FOUND")
    best = max(s for s, _ in scored)
    top = [it for s, it in scored if s == best]
    if len(top) > 1:
        raise Ambiguous("Tenés más de una publicación que coincide. ¿Cuál?", [_candidate(i) for i in top])
    return top[0]


_STOP = {
    "mi",
    "mis",
    "el",
    "la",
    "los",
    "las",
    "un",
    "una",
    "que",
    "tengo",
    "publicado",
    "publicacion",
    "borra",
    "borrar",
    "edita",
    "editar",
    "cambia",
    "cambiar",
    "del",
    "de",
}


def _candidate(item: Item) -> dict[str, Any]:
    return {"item_id": item.public_id, "title": item.title, "status": item.status.value}


# ---- edit / delete ----------------------------------------------------------------------------
def update_publication(
    session: Session,
    user: User,
    item_id: int,
    changes: dict[str, Any],
    *,
    new_image: bytes | None = None,
    storage: ImageStorage | None = None,
) -> Item:
    ensure_active(user)
    cfg.require_allowed(session, cfg.CriticalOp.EDIT)
    item = _get_owned(session, user, item_id)
    if item.status == ItemStatus.RESERVED:
        raise InvalidState("No podés editar mientras hay una oferta en curso.", code="ITEM_RESERVED")
    if item.status != ItemStatus.PUBLISHED:
        raise InvalidState("Solo se pueden editar publicaciones activas.", code="ITEM_NOT_EDITABLE")
    radical = RADICAL_FIELDS & set(changes)
    if radical:
        raise RadicalChange(
            "Si cambia el artículo, hay que cancelar esta publicación y crear una nueva.",
            details={"fields": sorted(radical)},
        )
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise ValidationFailed("campos no editables", code="FIELDS_NOT_EDITABLE", details={"fields": sorted(unknown)})
    texts = {
        k: clean_user_text(changes.get(k), 1000 if k == "description" else 500)
        for k in ("damage_description", "malfunction_description", "description")
        if k in changes
    }
    _check_texts(session, user, *texts.values())
    before = {k: _jsonable(getattr(item, "zone_id" if k == "zone" else k)) for k in changes}
    if "base_price" in changes:
        item.base_price = parse_price(changes["base_price"])
    if "zone" in changes:
        item.zone_id = resolve_zone(session, changes["zone"]).id
    for k in ("accepts_swap", "has_damage", "works"):
        if k in changes:
            if not isinstance(changes[k], bool):
                raise ValidationFailed(f"{k} debe ser sí/no", code="INVALID_FIELD")
            setattr(item, k, changes[k])
    for k, v in texts.items():
        setattr(item, k, v)
    if item.has_damage is False:
        item.damage_description = None
    if item.works is True:
        item.malfunction_description = None
    if item.has_damage and not item.damage_description:
        raise ValidationFailed("contanos cuáles son los daños", code="DAMAGE_DESCRIPTION_REQUIRED")
    if item.works is False and not item.malfunction_description:
        raise ValidationFailed("contanos qué problema tiene", code="MALFUNCTION_DESCRIPTION_REQUIRED")
    if new_image is not None:
        img = validate_images([new_image])
        _attach_image(session, item, img, storage or LocalImageStorage())
    _refresh_search_text(item)
    session.flush()
    after = {k: _jsonable(getattr(item, "zone_id" if k == "zone" else k)) for k in changes}
    details = {"before": before, "after": after, "image_replaced": new_image is not None}
    audit(
        session,
        AuditAction.ITEM_UPDATED,
        actor_type=ActorType.USER,
        actor_id=user.id,
        entity_type="item",
        entity_id=item.public_id,
        details=details,
    )
    get_bus().publish(
        session,
        EventType.ITEM_UPDATED,
        aggregate_type="item",
        aggregate_id=item.public_id,
        payload={"fields": sorted(changes)},
    )
    return item


def _jsonable(v: Any) -> Any:
    return str(v) if isinstance(v, Decimal) else v


def delete_publication(session: Session, user: User, item_id: int) -> Item:
    """Owner deletes. Allowed while an offer is pending or awaiting payment (offer is closed, nobody pays).
    Not allowed once the release fee was paid."""
    item = _get_owned(session, user, item_id)
    if item.status in (ItemStatus.COMPLETED, ItemStatus.EXPIRED, ItemStatus.DELETED):
        raise InvalidState("La publicación ya está cerrada.", code="ITEM_CLOSED")
    if item.status == ItemStatus.RESERVED:
        from app.services.offers.service import close_active_offer_for_item

        close_active_offer_for_item(
            session, item, reason=OfferCloseReason.ITEM_UNAVAILABLE, actor_type=ActorType.USER, actor_id=user.id
        )
        session.refresh(item)
    transition_item(
        session, item, ItemStatus.DELETED, actor_type=ActorType.USER, actor_id=user.id, reason="OWNER_DELETED"
    )
    return item


def admin_remove_item(session: Session, admin_id: int, public_id: str, reason: str) -> Item:
    item = session.execute(
        select(Item).where(Item.public_id == public_id).with_for_update().execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if item is None:
        raise NotFound("item not found", code="ITEM_NOT_FOUND")
    if item.status == ItemStatus.RESERVED:
        from app.services.offers.service import close_active_offer_for_item

        close_active_offer_for_item(
            session, item, reason=OfferCloseReason.ADMIN, actor_type=ActorType.ADMIN, actor_id=admin_id
        )
        session.refresh(item)
    transition_item(
        session, item, ItemStatus.DELETED, actor_type=ActorType.ADMIN, actor_id=admin_id, reason=f"ADMIN:{reason}"[:60]
    )
    return item


def get_by_public_id(session: Session, public_id: str, *, for_update: bool = False) -> Item:
    stmt = select(Item).where(Item.public_id == (public_id or "").strip().upper())
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    item = session.execute(stmt).scalar_one_or_none()
    if item is None:
        raise NotFound("No encontré ese artículo.", code="ITEM_NOT_FOUND")
    return item


def public_view(session: Session, item: Item) -> dict[str, Any]:
    """What another user may see before acceptance+payment: never owner identity/contact (section 18)."""
    zone = session.get(Zone, item.zone_id) if item.zone_id else None
    return {
        "item_id": item.public_id,
        "title": item.title,
        "category": item.category,
        "description": item.description,
        "zone": zone.name if zone else None,
        "price": str(item.base_price) if item.base_price is not None else None,
        "currency": item.currency,
        "accepts_swap": item.accepts_swap,
        "has_damage": item.has_damage,
        "damage_description": item.damage_description,
        "works": item.works,
        "malfunction_description": item.malfunction_description,
        "available": item.status == ItemStatus.PUBLISHED,
    }


def count_active(session: Session) -> int:
    return session.execute(
        select(func.count()).select_from(Item).where(Item.status == ItemStatus.PUBLISHED)
    ).scalar_one()
