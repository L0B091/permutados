import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.errors import Ambiguous, Forbidden, InvalidState, ValidationFailed
from app.models.audit import AuditLog
from app.models.enums import ItemStatus, UserStatus
from app.models.item import Item
from app.services.publications import service as pubs
from app.services.publications.image_validation import ImageRejected
from app.services.security.enforcement import EvasionBlocked
from tests.conftest import make_image


def test_full_publication_flow(db, make_user, image_bytes):
    owner = make_user("Ana")
    item = pubs.start_draft(db, owner, [image_bytes])
    assert item.status == ItemStatus.DRAFT and item.public_id.startswith("ITEM-") and len(item.images) == 1
    # The AI suggestion never sets a price, even if the model returns one.
    pubs.set_ai_identification(
        db, owner, item.id, {"title": "Taladro Bosch", "category": "herramientas", "price": "99999", "base_price": 1}
    )
    assert item.base_price is None and "price" not in item.ai_identification
    with pytest.raises(ValidationFailed) as e:
        pubs.publish(db, owner, item.id)
    assert e.value.code == "ITEM_INCOMPLETE"
    pubs.fill_draft(
        db,
        owner,
        item.id,
        identification_confirmed=True,
        zone="palermo",
        base_price="$45.000",
        accepts_swap=False,
        has_damage=True,
        damage_description="Raspón en el mango",
        works=True,
    )
    preview = pubs.preview_text(db, item)
    assert "ARS 45.000" in preview and "Palermo" in preview and "Raspón" in preview
    pubs.publish(db, owner, item.id)
    db.commit()
    assert item.status == ItemStatus.PUBLISHED
    assert (item.expires_at - item.published_at).days == 30
    actions = db.execute(select(AuditLog.action).where(AuditLog.entity_id == item.public_id)).scalars().all()
    assert "ITEM_CREATED" in actions


def test_invalid_image_does_not_create_draft(db, make_user):
    owner = make_user()
    with pytest.raises(ImageRejected):
        pubs.start_draft(db, owner, [b"not an image" * 500])
    with pytest.raises(ImageRejected):
        pubs.start_draft(db, owner, [make_image(seed=1), make_image(seed=2)])
    assert db.execute(select(Item)).first() is None


def test_unknown_zone_or_exact_address_rejected(db, make_user, image_bytes):
    owner = make_user()
    item = pubs.start_draft(db, owner, [image_bytes])
    with pytest.raises(ValidationFailed) as e:
        pubs.fill_draft(db, owner, item.id, zone="Av. Corrientes 1234 4B")
    assert e.value.code == "UNKNOWN_ZONE"


def test_edit_by_owner_only(db, make_user, publish_item):
    owner, other = make_user(), make_user()
    item = publish_item(owner)
    pubs.update_publication(
        db,
        owner,
        item.id,
        {"base_price": "120.000", "zone": "Belgrano", "works": False, "malfunction_description": "No enciende la luz"},
    )
    db.commit()
    assert str(item.base_price) == "120000.00" and item.works is False
    with pytest.raises(Forbidden):
        pubs.update_publication(db, other, item.id, {"base_price": "1"})


def test_radical_change_requires_new_publication(db, make_user, publish_item):
    owner = make_user()
    item = publish_item(owner)
    with pytest.raises(pubs.RadicalChange):
        pubs.update_publication(db, owner, item.id, {"title": "Heladera"})


def test_edit_replaces_single_image(db, make_user, publish_item):
    owner = make_user()
    item = publish_item(owner)
    old_sha = item.images[0].sha256
    pubs.update_publication(db, owner, item.id, {}, new_image=make_image(seed=99))
    db.commit()
    db.refresh(item)
    assert len(item.images) == 1 and item.images[0].sha256 != old_sha


def test_delete_and_resolution_by_owner(db, make_user, publish_item):
    owner = make_user()
    bici = publish_item(owner, title="Bicicleta playera roja")
    publish_item(owner, title="Taladro percutor", category="herramientas")
    assert pubs.resolve_owned_item(db, owner, "borrá el taladro").title == "Taladro percutor"
    pubs.delete_publication(db, owner, pubs.resolve_owned_item(db, owner, "mi bicicleta").id)
    db.commit()
    assert bici.status == ItemStatus.DELETED
    assert [i.title for i in pubs.list_my_publications(db, owner)] == ["Taladro percutor"]


def test_ambiguous_owner_reference_asks(db, make_user, publish_item):
    owner = make_user()
    publish_item(owner, title="Bicicleta roja")
    publish_item(owner, title="Bicicleta azul")
    with pytest.raises(Ambiguous) as e:
        pubs.resolve_owned_item(db, owner, "editá mi bicicleta")
    assert len(e.value.candidates) == 2


def test_closed_item_cannot_be_deleted_again(db, make_user, publish_item):
    owner = make_user()
    item = publish_item(owner)
    pubs.delete_publication(db, owner, item.id)
    with pytest.raises(InvalidState):
        pubs.delete_publication(db, owner, item.id)


def test_public_id_and_owner_are_immutable_in_db(db, make_user, publish_item):
    owner, other = make_user(), make_user()
    item = publish_item(owner)
    with pytest.raises(DBAPIError, match="immutable"):
        db.execute(text("UPDATE items SET public_id='ITEM-ZZZZZ' WHERE id=:i"), {"i": item.id})
    db.rollback()
    with pytest.raises(DBAPIError, match="immutable"):
        db.execute(text("UPDATE items SET owner_id=:o WHERE id=:i"), {"o": other.id, "i": item.id})


def test_contact_data_in_description_is_blocked_and_counted(db, make_user, image_bytes):
    owner = make_user()
    item = pubs.start_draft(db, owner, [image_bytes])
    db.commit()
    with pytest.raises(EvasionBlocked) as e:
        pubs.fill_draft(db, owner, item.id, description="Escribime al 11 4444 5555")
    assert e.value.verdict.action.value == "WARNED"
    db.rollback()
    db.refresh(owner)
    assert owner.evasion_strikes == 1 and owner.status == UserStatus.ACTIVE  # survived the rollback
    db.refresh(item)
    assert item.description is None
