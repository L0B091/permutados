"""Domain errors. Each carries a stable machine code used by Dolphin tools and the API."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code: str = "DOMAIN_ERROR"
    http_status: int = 400

    def __init__(self, message: str = "", *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        if code:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


class NotFound(DomainError):
    code = "NOT_FOUND"
    http_status = 404


class Forbidden(DomainError):
    code = "FORBIDDEN"
    http_status = 403


class InvalidState(DomainError):
    code = "INVALID_STATE"
    http_status = 409


class Conflict(DomainError):
    code = "CONFLICT"
    http_status = 409


class ValidationFailed(DomainError):
    code = "VALIDATION_FAILED"
    http_status = 422


class Ambiguous(DomainError):
    """More than one candidate matched. The caller must ask the user to disambiguate."""

    code = "AMBIGUOUS"
    http_status = 409

    def __init__(self, message: str, candidates: list[dict[str, Any]]):
        super().__init__(message, details={"candidates": candidates})
        self.candidates = candidates


class SystemModeBlocked(DomainError):
    code = "SYSTEM_MODE_BLOCKED"
    http_status = 503


class UserBlocked(DomainError):
    code = "USER_BLOCKED"
    http_status = 403


class ExternalServiceError(DomainError):
    code = "EXTERNAL_SERVICE_ERROR"
    http_status = 502
