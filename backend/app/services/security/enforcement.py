"""Backend-owned enforcement of the anti-evasion policy.

1st attempt  -> warning that explicitly says the next attempt will be penalised.
2nd attempt+ -> account SUSPENDED + ban record PENDING_REVIEW. An admin confirms (BANNED) or lifts it.
The counter lives in ``users.evasion_strikes`` and is updated under a row lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from app.models.enums import ActorType, BanStatus, OfferCloseReason, SecurityEventType, Severity, UserStatus
from app.models.security import Ban, SecurityEvent, Warning
from app.services.audit.service import AuditAction, audit
from app.services.events.bus import EventType, get_bus
from app.services.notifications import templates
from app.services.security.anti_evasion import EvasionResult, detect
from app.services.users.service import get_user


class EvasionAction(StrEnum):
    NONE = "NONE"
    WARNED = "WARNED"
    SUSPENDED = "SUSPENDED"


@dataclass(frozen=True)
class EvasionVerdict:
    action: EvasionAction
    message: str | None = None
    kinds: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.action != EvasionAction.NONE


def record_security_event(
    session: Session,
    *,
    user_id: int | None,
    event_type: SecurityEventType,
    severity: Severity,
    details: dict | None = None,
    message_id: int | None = None,
) -> SecurityEvent:
    ev = SecurityEvent(
        user_id=user_id, event_type=event_type, severity=severity, details=details or {}, message_id=message_id
    )
    session.add(ev)
    session.flush()
    return ev


def inspect_and_enforce(
    session: Session, user_id: int, text: str | None, *, source: str, message_id: int | None = None
) -> EvasionVerdict:
    result: EvasionResult = detect(text)
    if not result.detected:
        return EvasionVerdict(EvasionAction.NONE)
    return register_attempt(session, user_id, result, source=source, message_id=message_id)


def register_attempt(
    session: Session, user_id: int, result: EvasionResult, *, source: str, message_id: int | None = None
) -> EvasionVerdict:
    user = get_user(session, user_id, for_update=True)
    user.evasion_strikes += 1
    event = record_security_event(
        session,
        user_id=user.id,
        event_type=SecurityEventType.EVASION_ATTEMPT,
        severity=Severity.MEDIUM if user.evasion_strikes == 1 else Severity.HIGH,
        details={"kinds": result.kinds, "source": source, "strike": user.evasion_strikes},
        message_id=message_id,
    )
    kinds = tuple(result.kinds)
    if user.evasion_strikes == 1:
        user.warnings_count += 1
        session.add(
            Warning(
                user_id=user.id, reason="EVASION", message_sent=templates.EVASION_WARNING, security_event_id=event.id
            )
        )
        audit(
            session,
            AuditAction.USER_WARNED,
            actor_type=ActorType.SYSTEM,
            entity_type="user",
            entity_id=user.id,
            details={"kinds": list(kinds), "source": source},
        )
        session.flush()
        return EvasionVerdict(EvasionAction.WARNED, templates.EVASION_WARNING, kinds)

    suspend_pending_review(
        session,
        user.id,
        reason="EVASION_REPEATED",
        security_event_id=event.id,
        details={"kinds": list(kinds), "source": source},
    )
    return EvasionVerdict(EvasionAction.SUSPENDED, templates.EVASION_SUSPENDED, kinds)


def suspend_pending_review(
    session: Session,
    user_id: int,
    *,
    reason: str,
    security_event_id: int | None = None,
    details: dict | None = None,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_id: int | None = None,
) -> None:
    from sqlalchemy import select

    user = get_user(session, user_id, for_update=True)
    if user.status == UserStatus.ACTIVE:
        user.status = UserStatus.SUSPENDED
    open_ban = session.execute(
        select(Ban).where(Ban.user_id == user.id, Ban.status.in_([BanStatus.PENDING_REVIEW, BanStatus.CONFIRMED]))
    ).scalar_one_or_none()
    if open_ban is None:
        session.add(
            Ban(user_id=user.id, status=BanStatus.PENDING_REVIEW, reason=reason, security_event_id=security_event_id)
        )
    audit(
        session,
        AuditAction.USER_SUSPENDED,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type="user",
        entity_id=user.id,
        details={"reason": reason, **(details or {})},
    )
    session.flush()
    # Close the user's open, unpaid offers so no item stays paused by a suspended account.
    from app.services.offers.service import close_active_offers_for_user

    close_active_offers_for_user(session, user.id, reason=OfferCloseReason.USER_BLOCKED)
    get_bus().publish(
        session, EventType.USER_SUSPENDED, aggregate_type="user", aggregate_id=user.id, payload={"reason": reason}
    )


class EvasionBlocked(Exception):
    """Raised after a blocked attempt. The strike/warning was already COMMITTED so it survives the
    rollback of the operation that contained the contact data."""

    code = "EVASION_BLOCKED"
    http_status = 422

    def __init__(self, verdict: EvasionVerdict):
        super().__init__(verdict.message or "EVASION_BLOCKED")
        self.verdict = verdict
        self.message = verdict.message or templates.EVASION_WARNING
        self.details = {"action": verdict.action.value, "kinds": list(verdict.kinds)}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


def enforce_or_raise(
    session: Session, user_id: int, *texts: str | None, source: str, message_id: int | None = None
) -> None:
    """Check free text before any mutation. On detection: persist the strike (commit) and raise.

    Must be called BEFORE the calling operation writes anything, because it commits the session.
    """
    joined = "\n".join(t for t in texts if t)
    verdict = inspect_and_enforce(session, user_id, joined, source=source, message_id=message_id)
    if verdict.blocked:
        session.commit()
        raise EvasionBlocked(verdict)
