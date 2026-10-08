"""Admin authentication: Argon2id passwords, short-lived JWT access tokens, rotating refresh tokens with
reuse detection, account lockout, audit of every login attempt."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.errors import DomainError, ValidationFailed
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    hash_password,
    hash_token,
    new_refresh_token,
    verify_password,
)
from app.models.enums import ActorType, AdminRole, SecurityEventType, Severity
from app.models.system import AdminRefreshToken, AdminUser
from app.services.audit.service import AuditAction, audit
from app.services.security.enforcement import record_security_event


class AuthError(DomainError):
    code = "INVALID_CREDENTIALS"
    http_status = 401


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    role: str


ROLE_RANK = {AdminRole.VIEWER: 0, AdminRole.MODERATOR: 1, AdminRole.ADMIN: 2, AdminRole.SUPERADMIN: 3}


def create_admin(session: Session, username: str, password: str, role: AdminRole) -> AdminUser:
    username = username.strip().lower()
    if not username or len(username) > 60:
        raise ValidationFailed("invalid username")
    admin = AdminUser(username=username, password_hash=hash_password(password), role=role)
    session.add(admin)
    session.flush()
    audit(
        session,
        AuditAction.ADMIN_ACTION,
        actor_type=ActorType.SYSTEM,
        entity_type="admin_user",
        entity_id=admin.id,
        details={"action": "created", "role": role.value},
    )
    return admin


def _issue(session: Session, admin: AdminUser, family: str, *, ip: str | None, ua: str | None) -> TokenPair:
    s = get_settings()
    refresh = new_refresh_token()
    session.add(
        AdminRefreshToken(
            admin_id=admin.id,
            token_hash=hash_token(refresh),
            family_id=family,
            expires_at=utcnow() + timedelta(days=s.admin_refresh_token_days),
            ip_address=ip,
            user_agent=(ua or "")[:255] or None,
        )
    )
    session.flush()
    return TokenPair(
        create_access_token(admin.id, admin.role.value, session_family=family),
        refresh,
        s.admin_access_token_minutes * 60,
        admin.role.value,
    )


def login(
    session: Session, username: str, password: str, *, ip: str | None = None, user_agent: str | None = None
) -> TokenPair:
    s = get_settings()
    now = utcnow()
    admin = session.execute(
        select(AdminUser)
        .where(AdminUser.username == (username or "").strip().lower())
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if admin is None:
        verify_password(DUMMY_PASSWORD_HASH, password or "")  # equalise timing
        audit(
            session,
            AuditAction.ADMIN_LOGIN_FAILED,
            actor_type=ActorType.ADMIN,
            ip_address=ip,
            details={"reason": "unknown_user"},
        )
        raise AuthError("Usuario o contraseña incorrectos.")
    if not admin.active:
        audit(
            session,
            AuditAction.ADMIN_LOGIN_FAILED,
            actor_type=ActorType.ADMIN,
            actor_id=admin.id,
            ip_address=ip,
            details={"reason": "inactive"},
        )
        raise AuthError("Usuario o contraseña incorrectos.")
    if admin.locked_until and admin.locked_until > now:
        audit(
            session,
            AuditAction.ADMIN_LOGIN_FAILED,
            actor_type=ActorType.ADMIN,
            actor_id=admin.id,
            ip_address=ip,
            details={"reason": "locked"},
        )
        raise AuthError("Cuenta bloqueada temporalmente por intentos fallidos.", code="ACCOUNT_LOCKED")
    if not verify_password(admin.password_hash, password or ""):
        admin.failed_login_count += 1
        if admin.failed_login_count >= s.admin_max_failed_logins:
            admin.locked_until = now + timedelta(minutes=s.admin_lockout_minutes)
            admin.failed_login_count = 0
        record_security_event(
            session,
            user_id=None,
            event_type=SecurityEventType.ADMIN_LOGIN_FAILED,
            severity=Severity.MEDIUM,
            details={"admin_id": admin.id},
        )
        audit(
            session,
            AuditAction.ADMIN_LOGIN_FAILED,
            actor_type=ActorType.ADMIN,
            actor_id=admin.id,
            ip_address=ip,
            details={"reason": "bad_password"},
        )
        raise AuthError("Usuario o contraseña incorrectos.")
    admin.failed_login_count = 0
    admin.locked_until = None
    admin.last_login_at = now
    pair = _issue(session, admin, uuid.uuid4().hex, ip=ip, ua=user_agent)
    audit(session, AuditAction.ADMIN_LOGIN, actor_type=ActorType.ADMIN, actor_id=admin.id, ip_address=ip)
    return pair


def refresh(session: Session, refresh_token: str, *, ip: str | None = None, user_agent: str | None = None) -> TokenPair:
    now = utcnow()
    row = session.execute(
        select(AdminRefreshToken)
        .where(AdminRefreshToken.token_hash == hash_token(refresh_token or ""))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise AuthError("Sesión inválida.", code="INVALID_REFRESH_TOKEN")
    if row.revoked_at is not None:
        # Reuse of a rotated token => likely theft: revoke the whole family.
        session.execute(
            update(AdminRefreshToken)
            .where(AdminRefreshToken.family_id == row.family_id, AdminRefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        audit(
            session,
            AuditAction.ADMIN_ACTION,
            actor_type=ActorType.ADMIN,
            actor_id=row.admin_id,
            ip_address=ip,
            details={"action": "refresh_token_reuse_detected"},
        )
        session.commit()
        raise AuthError("Sesión inválida.", code="REFRESH_TOKEN_REUSED")
    if row.expires_at <= now:
        raise AuthError("Sesión expirada.", code="REFRESH_TOKEN_EXPIRED")
    admin = session.get(AdminUser, row.admin_id)
    if admin is None or not admin.active:
        raise AuthError("Sesión inválida.", code="INVALID_REFRESH_TOKEN")
    row.revoked_at = now
    pair = _issue(session, admin, row.family_id, ip=ip, ua=user_agent)
    new_row = session.execute(
        select(AdminRefreshToken).where(AdminRefreshToken.token_hash == hash_token(pair.refresh_token))
    ).scalar_one()
    row.replaced_by_id = new_row.id
    return pair


def logout(session: Session, admin_id: int, family_id: str, *, ip: str | None = None) -> None:
    session.execute(
        update(AdminRefreshToken)
        .where(
            AdminRefreshToken.admin_id == admin_id,
            AdminRefreshToken.family_id == family_id,
            AdminRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    audit(session, AuditAction.ADMIN_LOGOUT, actor_type=ActorType.ADMIN, actor_id=admin_id, ip_address=ip)


def is_family_active(session: Session, family_id: str) -> bool:
    """Access tokens die with their session family (logout / reuse detection)."""
    return (
        session.execute(
            select(AdminRefreshToken.id).where(
                AdminRefreshToken.family_id == family_id,
                AdminRefreshToken.revoked_at.is_(None),
                AdminRefreshToken.expires_at > utcnow(),
            )
        ).first()
        is not None
    )
