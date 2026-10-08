from __future__ import annotations

import ipaddress
from collections.abc import Iterator
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.core.security import decode_access_token
from app.models.enums import AdminRole
from app.models.system import AdminUser
from app.services.admin.auth import ROLE_RANK, is_family_active

_bearer = HTTPBearer(auto_error=False)


def get_db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def client_ip(request: Request) -> str | None:
    # Behind the reverse proxy (see docker/nginx.conf) the real IP arrives in X-Forwarded-For; uvicorn is
    # started with --proxy-headers --forwarded-allow-ips limited to the proxy.
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


@dataclass
class AdminPrincipal:
    id: int
    role: AdminRole
    family: str
    username: str


def current_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)
) -> AdminPrincipal:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail="not authenticated", headers={"WWW-Authenticate": "Bearer"}
    )
    if creds is None or creds.scheme.lower() != "bearer":
        raise unauthorized
    try:
        payload = decode_access_token(creds.credentials)
    except jwt.PyJWTError:
        raise unauthorized from None
    admin = db.get(AdminUser, int(payload["sub"]))
    if admin is None or not admin.active or not is_family_active(db, payload.get("fam", "")):
        raise unauthorized
    # Role is always taken from the DB (a demoted admin loses rights immediately).
    return AdminPrincipal(id=admin.id, role=admin.role, family=payload["fam"], username=admin.username)


def require_role(minimum: AdminRole):
    def dep(admin: AdminPrincipal = Depends(current_admin)) -> AdminPrincipal:
        if ROLE_RANK[admin.role] < ROLE_RANK[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="insufficient role")
        return admin

    return dep


def payment_gateway(request: Request):
    return getattr(request.app.state, "payment_gateway", None)
