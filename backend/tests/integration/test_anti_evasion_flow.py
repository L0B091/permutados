from sqlalchemy import select

from app.models.enums import BanStatus, ItemStatus, OfferStatus, UserStatus
from app.models.security import Ban, Warning
from app.services.admin import service as admin_service
from app.services.conversation.service import handle_inbound
from app.services.dolphin.mock import MockDolphinClient
from app.services.notifications import templates
from app.services.offers import service as offers


def test_first_warning_then_suspension_pending_admin_review(db, make_user, publish_item):
    user = make_user()
    owner = make_user()
    item = publish_item(owner)
    offer = offers.create_offer(db, user, item.public_id, offer_type="MONEY", offered_price="1000").offer
    db.commit()
    dolphin = MockDolphinClient()

    r1 = handle_inbound(
        db, wa_id=user.test_wa_id, text="escribime al 11 5555 4444", client=dolphin, provider_message_id="wamid.1"
    )
    db.commit()
    assert r1.blocked and r1.replies == [templates.EVASION_WARNING]
    assert "próxima vez" in r1.replies[0]
    db.refresh(user)
    assert user.status == UserStatus.ACTIVE and user.evasion_strikes == 1
    assert len(db.execute(select(Warning).where(Warning.user_id == user.id)).scalars().all()) == 1
    assert dolphin.requests == []  # the AI never saw the blocked message

    r2 = handle_inbound(
        db,
        wa_id=user.test_wa_id,
        text="dale, buscame en instagram @juan",
        client=dolphin,
        provider_message_id="wamid.2",
    )
    db.commit()
    assert r2.blocked and r2.replies == [templates.EVASION_SUSPENDED]
    db.refresh(user)
    assert user.status == UserStatus.SUSPENDED  # not permanently banned: an admin decides
    ban = db.execute(select(Ban).where(Ban.user_id == user.id)).scalar_one()
    assert ban.status == BanStatus.PENDING_REVIEW
    db.refresh(offer)
    db.refresh(item)
    assert offer.status == OfferStatus.CANCELLED and item.status == ItemStatus.PUBLISHED  # item not left paused

    r3 = handle_inbound(db, wa_id=user.test_wa_id, text="hola?", client=dolphin, provider_message_id="wamid.3")
    assert r3.blocked and r3.replies == [templates.ACCOUNT_SUSPENDED] and dolphin.requests == []


def test_admin_confirms_or_lifts(db, make_user):
    from app.models.enums import AdminRole
    from app.services.admin.auth import create_admin

    admin = create_admin(db, "mod1", "moderator-password-1", AdminRole.MODERATOR)
    u1, u2 = make_user(), make_user()
    for u in (u1, u2):
        for i in range(2):
            handle_inbound(
                db,
                wa_id=u.test_wa_id,
                text="mi mail es a@b.com",
                client=MockDolphinClient(),
                provider_message_id=f"m-{u.id}-{i}",
            )
    db.commit()
    admin_service.confirm_ban(db, admin.id, u1.id, "reincidente")
    admin_service.reinstate(db, admin.id, u2.id, "falso positivo", reset_strikes=True)
    db.commit()
    db.refresh(u1)
    db.refresh(u2)
    assert u1.status == UserStatus.BANNED and u2.status == UserStatus.ACTIVE and u2.evasion_strikes == 0


def test_duplicate_webhook_message_processed_once(db, make_user):
    u = make_user()
    d = MockDolphinClient()
    handle_inbound(db, wa_id=u.test_wa_id, text="hola", client=d, provider_message_id="wamid.X")
    db.commit()
    r = handle_inbound(db, wa_id=u.test_wa_id, text="hola", client=d, provider_message_id="wamid.X")
    assert r.duplicate and len(d.requests) == 1


def test_legit_prices_are_not_penalised(db, make_user):
    u = make_user()
    r = handle_inbound(
        db,
        wa_id=u.test_wa_id,
        text="te ofrezco $150.000 o 1.500.000 pesos por las dos",
        client=MockDolphinClient(),
        provider_message_id="w1",
    )
    db.commit()
    db.refresh(u)
    assert not r.blocked and u.evasion_strikes == 0
