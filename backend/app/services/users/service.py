from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.errors import NotFound, UserBlocked, ValidationFailed
from app.models.enums import ActorType, UserStatus
from app.models.user import User
from app.services.audit.service import AuditAction, audit

_WA_ID = re.compile(r"^[0-9]{6,20}$")


def normalize_whatsapp_id(wa_id: str) -> str:
    wa = re.sub(r"[^\d]", "", wa_id or "")
    if not _WA_ID.match(wa):
        raise ValidationFailed("invalid WhatsApp id", code="INVALID_WHATSAPP_ID")
    return wa


def get_or_create_by_whatsapp(session: Session, wa_id: str, display_name: str | None = None) -> User:
    """Identity is always the WhatsApp ID, never the profile name."""
    wa = normalize_whatsapp_id(wa_id)
    stmt = (
        insert(User)
        .values(whatsapp_id=wa, phone=wa, display_name=(display_name or None) and display_name[:120])
        .on_conflict_do_nothing(index_elements=["whatsapp_id"])
        .returning(User.id)
    )
    new_id = session.execute(stmt).scalar_one_or_none()
    user = session.execute(select(User).where(User.whatsapp_id == wa)).scalar_one()
    if new_id is not None:
        audit(session, AuditAction.USER_CREATED, actor_type=ActorType.SYSTEM, entity_type="user", entity_id=user.id)
    return user


def get_user(session: Session, user_id: int, *, for_update: bool = False) -> User:
    stmt = select(User).where(User.id == user_id)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    user = session.execute(stmt).scalar_one_or_none()
    if user is None:
        raise NotFound("user not found", code="USER_NOT_FOUND")
    return user


def ensure_active(user: User) -> None:
    if user.status != UserStatus.ACTIVE:
        raise UserBlocked(
            "Tu cuenta está suspendida y en revisión por un administrador.",
            details={"status": user.status.value},
        )
