"""FastAPI application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import health, webhooks
from app.api.routes.admin import auth as admin_auth
from app.api.routes.admin import console as admin_console
from app.api.routes.admin import routes as admin_routes
from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.logging import configure_logging
from app.services.security.enforcement import EvasionBlocked

log = logging.getLogger(__name__)


def _default_gateway():
    s = get_settings()
    if s.mercadopago_access_token.get_secret_value():
        from app.services.mercadopago.client import MercadoPagoGateway

        return MercadoPagoGateway()
    return None


def create_app(*, payment_gateway=None) -> FastAPI:
    settings = get_settings()
    configure_logging()
    if settings.is_production:
        problems = settings.validate_for_production()
        if problems:
            raise RuntimeError("unsafe production configuration: " + "; ".join(problems))
    app = FastAPI(
        title="Permutados API",
        version="0.1.0",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    gw = payment_gateway if payment_gateway is not None else _default_gateway()
    if settings.is_production and gw is not None and type(gw).__name__.startswith("Fake"):
        raise RuntimeError("fake payment gateway is not allowed in production")
    app.state.payment_gateway = gw

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_: Request, exc: DomainError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(EvasionBlocked)
    async def evasion(_: Request, exc: EvasionBlocked):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code, content={"error": str(exc.detail)}, headers=getattr(exc, "headers", None)
        )

    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(admin_auth.router)
    app.include_router(admin_routes.router)
    app.include_router(admin_console.router)
    return app
