import pytest

from app.core.errors import SystemModeBlocked
from app.models.enums import SystemMode
from app.services.offers import service as offers
from app.services.publications import service as pubs
from app.services.search import service as search
from app.services.system import config_service as cfg
from tests.conftest import make_image


def test_paused_blocks_offers_and_acceptance_but_not_publish_or_search(db, make_user, publish_item):
    owner, bidder = make_user(), make_user()
    item = publish_item(owner)
    pending = offers.create_offer(db, bidder, item.public_id, offer_type="MONEY", offered_price="10").offer
    other_item = publish_item(owner, title="Otra cosa")
    cfg.set_value(db, "system_mode", SystemMode.PAUSED.value)
    db.commit()
    with pytest.raises(SystemModeBlocked):
        offers.create_offer(db, bidder, other_item.public_id, offer_type="MONEY", offered_price="10")
    with pytest.raises(SystemModeBlocked):
        offers.accept_offer(db, owner, pending.public_id)
    publish_item(owner, title="Publicable en pausa")
    assert search.start_search(db, bidder, "publicable").results


def test_emergency_blocks_publication_and_edits(db, make_user, publish_item):
    owner = make_user()
    item = publish_item(owner)
    cfg.set_value(db, "system_mode", SystemMode.EMERGENCY.value)
    db.commit()
    with pytest.raises(SystemModeBlocked):
        pubs.start_draft(db, owner, [make_image()])
    with pytest.raises(SystemModeBlocked):
        pubs.update_publication(db, owner, item.id, {"base_price": "5"})


def test_invalid_config_values_rejected(db):
    from app.core.errors import ValidationFailed

    for key, value in [
        ("contact_release_price", "-1"),
        ("contact_release_currency", "USD"),
        ("offer_expiration_hours", 0),
        ("system_mode", "PANIC"),
        ("nope", 1),
    ]:
        with pytest.raises(ValidationFailed):
            cfg.set_value(db, key, value)
    assert cfg.set_value(db, "contact_release_price", 3500) == "3500.00"
