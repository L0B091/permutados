from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.errors import NotFound, UserBlocked
from app.models.enums import ActorType, UserStatus
from app.models.user import User
from app.services.audit.service import AuditAction, audit
from app.services.users.pii import WaIdentity, encrypt_display_name, identity_from_wa_id, whatsapp_hash

__all__ = ["get_or_create_by_whatsapp", "get_or_create_by_identity", "find_by_whatsapp", "get_user", "ensure_active"]


def get_or_create_by_whatsapp(session: Session, wa_id: str, display_name: str | None = None) -> User:
    """Identity is always the WhatsApp ID, never the profile name. The number is encrypted immediately."""
    return get_or_create_by_identity(session, identity_from_wa_id(wa_id), display_name)


def get_or_create_by_identity(session: Session, identity: WaIdentity, display_name: str | None = None) -> User:
    stmt = (
        insert(User)
        .values(
            whatsapp_hash=identity.hash,
            whatsapp_enc=identity.enc,
            whatsapp_last4=identity.last4,
            display_name_enc=encrypt_display_name(display_name),
        )
        .on_conflict_do_nothing(index_elements=["whatsapp_hash"])
        .returning(User.id)
    )
    new_id = session.execute(stmt).scalar_one_or_none()
    user = session.execute(select(User).where(User.whatsapp_hash == identity.hash)).scalar_one()
    if new_id is not None:
        audit(session, AuditAction.USER_CREATED, actor_type=ActorType.SYSTEM, entity_type="user", entity_id=user.id)
    return user


def find_by_whatsapp(session: Session, wa_id: str) -> User | None:
    return session.execute(select(User).where(User.whatsapp_hash == whatsapp_hash(wa_id))).scalar_one_or_none()


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
