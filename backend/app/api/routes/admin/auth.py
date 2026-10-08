from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import AdminPrincipal, client_ip, current_admin, get_db
from app.core.config import get_settings
from app.core.ratelimit import get_rate_limiter
from app.services.admin import auth as auth_service

router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=60)
    password: str = Field(..., min_length=1, max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str = Field(..., min_length=10, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int
    role: str


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    ip = client_ip(request) or "unknown"
    limit = get_settings().admin_login_rate_limit_per_minute
    limiter = get_rate_limiter()
    if not limiter.hit(f"login:ip:{ip}", limit) or not limiter.hit(f"login:user:{body.username.lower()}", limit):
        raise HTTPException(429, "too many attempts", headers={"Retry-After": "60"})
    try:
        pair = auth_service.login(
            db, body.username, body.password, ip=client_ip(request), user_agent=request.headers.get("user-agent")
        )
    except auth_service.AuthError:
        db.commit()  # keep the failed-attempt counter and audit trail
        raise
    return TokenOut(
        access_token=pair.access_token, refresh_token=pair.refresh_token, expires_in=pair.expires_in, role=pair.role
    )


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, request: Request, db: Session = Depends(get_db)) -> TokenOut:
    ip = client_ip(request) or "unknown"
    if not get_rate_limiter().hit(f"refresh:ip:{ip}", 30):
        raise HTTPException(429, "too many attempts")
    pair = auth_service.refresh(
        db, body.refresh_token, ip=client_ip(request), user_agent=request.headers.get("user-agent")
    )
    return TokenOut(
        access_token=pair.access_token, refresh_token=pair.refresh_token, expires_in=pair.expires_in, role=pair.role
    )


@router.post("/logout", status_code=204)
def logout(request: Request, admin: AdminPrincipal = Depends(current_admin), db: Session = Depends(get_db)) -> None:
    auth_service.logout(db, admin.id, admin.family, ip=client_ip(request))


@router.get("/me")
def me(admin: AdminPrincipal = Depends(current_admin)) -> dict:
    return {"id": admin.id, "username": admin.username, "role": admin.role.value}
