"""Admin moderation actions. Every action is audited with the admin id."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.errors import InvalidState, NotFound
from app.models.enums import ActorType, BanStatus, UserStatus
from app.models.security import Ban
from app.models.user import User
from app.services.audit.service import AuditAction, audit
from app.services.security.enforcement import suspend_pending_review
from app.services.users.service import get_user


def suspend_user(session: Session, admin_id: int, user_id: int, reason: str) -> User:
    suspend_pending_review(
        session, user_id, reason=f"ADMIN:{reason}"[:60], actor_type=ActorType.ADMIN, actor_id=admin_id
    )
    audit(
        session,
        AuditAction.ADMIN_SUSPENDED_USER,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="user",
        entity_id=user_id,
        details={"reason": reason},
    )
    return get_user(session, user_id)


def confirm_ban(session: Session, admin_id: int, user_id: int, notes: str | None = None) -> User:
    user = get_user(session, user_id, for_update=True)
    ban = session.execute(
        select(Ban)
        .where(Ban.user_id == user.id, Ban.status == BanStatus.PENDING_REVIEW)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if ban is None:
        existing = session.execute(
            select(Ban).where(Ban.user_id == user.id, Ban.status == BanStatus.CONFIRMED)
        ).scalar_one_or_none()
        if existing:
            return user
        ban = Ban(user_id=user.id, status=BanStatus.PENDING_REVIEW, reason="ADMIN_DIRECT")
        session.add(ban)
        session.flush()
    ban.status = BanStatus.CONFIRMED
    ban.reviewed_by_admin_id = admin_id
    ban.reviewed_at = utcnow()
    ban.review_notes = notes
    user.status = UserStatus.BANNED
    session.flush()
    audit(
        session,
        AuditAction.USER_BANNED,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="user",
        entity_id=user.id,
    )
    audit(
        session,
        AuditAction.ADMIN_BANNED_USER,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="user",
        entity_id=user.id,
        details={"notes": notes},
    )
    return user


def reinstate(
    session: Session, admin_id: int, user_id: int, notes: str | None = None, *, reset_strikes: bool = False
) -> User:
    user = get_user(session, user_id, for_update=True)
    if user.status == UserStatus.ACTIVE:
        raise InvalidState("user is already active", code="USER_ACTIVE")
    for ban in session.execute(
        select(Ban)
        .where(Ban.user_id == user.id, Ban.status.in_([BanStatus.PENDING_REVIEW, BanStatus.CONFIRMED]))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars():
        ban.status = BanStatus.LIFTED
        ban.reviewed_by_admin_id = admin_id
        ban.reviewed_at = utcnow()
        ban.review_notes = notes
    user.status = UserStatus.ACTIVE
    if reset_strikes:
        user.evasion_strikes = 0
    session.flush()
    audit(
        session,
        AuditAction.USER_REINSTATED,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="user",
        entity_id=user.id,
    )
    audit(
        session,
        AuditAction.ADMIN_REINSTATED_USER,
        actor_type=ActorType.ADMIN,
        actor_id=admin_id,
        entity_type="user",
        entity_id=user.id,
        details={"notes": notes, "reset_strikes": reset_strikes},
    )
    return user


def find_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFound("user not found", code="USER_NOT_FOUND")
    return user
