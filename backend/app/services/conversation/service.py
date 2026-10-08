"""Inbound message pipeline (deterministic gates before Dolphin):

1. identify user by WhatsApp ID; dedupe by provider message id (webhook retries)
2. suspended/banned users get a fixed reply (no AI)
3. per-user rate limit
4. anti-evasion on the raw text (strike + warning/suspension; message blocked, never forwarded)
5. images: strict validation before anything else ("no avanzar hasta tener imagen válida")
6. state reconciled from the database + deterministic intent detection (hint for Dolphin)
7. "más / siguiente / ver más" served deterministically from the active search session
8. otherwise Dolphin runs the turn through allowlisted tools (the backend validates and decides)
Replies are written to the notifications outbox with idempotency keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.ratelimit import get_rate_limiter
from app.models.conversation import ConversationSession, Message
from app.models.enums import MessageDirection, MessageType, SecurityEventType, Severity, UserStatus
from app.services.conversation import state_machine
from app.services.conversation.intents import Intent, detect_intent
from app.services.dolphin.client import DolphinClient
from app.services.dolphin.orchestrator import run_turn
from app.services.mercadopago.gateway import PaymentGateway
from app.services.notifications import templates
from app.services.notifications.outbox import reopen_window
from app.services.notifications.service import enqueue
from app.services.publications.image_validation import ImageRejected, validate_images
from app.services.publications.storage import ImageStorage
from app.services.search import service as search
from app.services.security.anti_evasion import Detection, EvasionResult
from app.services.security.enforcement import inspect_and_enforce, record_security_event, register_attempt
from app.services.users.pii import WaIdentity, identity_from_wa_id, seal_message_evidence
from app.services.users.service import get_or_create_by_identity


@dataclass
class InboundResult:
    replies: list[str] = field(default_factory=list)
    duplicate: bool = False
    blocked: bool = False
    used_dolphin: bool = False


def get_active_session(session: Session, user_id: int) -> ConversationSession:
    session.execute(
        insert(ConversationSession)
        .values(user_id=user_id, active=True)
        .on_conflict_do_nothing(index_elements=["user_id"], index_where=ConversationSession.active)
    )
    return session.execute(
        select(ConversationSession)
        .where(ConversationSession.user_id == user_id, ConversationSession.active.is_(True))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


def _reply(session: Session, conv: ConversationSession, user_id: int, inbound: Message, texts: list[str]) -> None:
    for n, text in enumerate(t for t in texts if t):
        session.add(
            Message(
                session_id=conv.id,
                user_id=user_id,
                direction=MessageDirection.OUTBOUND,
                message_type=MessageType.TEXT,
                body=text,
            )
        )
        enqueue(session, user_id=user_id, kind="REPLY", body=text, idempotency_key=f"reply:{inbound.id}:{n}")
    session.flush()


def handle_inbound(
    session: Session,
    *,
    wa_id: str | None = None,
    identity: WaIdentity | None = None,
    text: str | None,
    images: list[bytes] | None = None,
    image_mime: str | None = None,
    message_type: str | None = None,
    provider_message_id: str | None = None,
    profile_name: str | None = None,
    media_failed: bool = False,
    client: DolphinClient,
    gateway: PaymentGateway | None = None,
    storage: ImageStorage | None = None,
) -> InboundResult:
    if identity is None:
        if wa_id is None:
            raise ValueError("wa_id or identity is required")
        identity = identity_from_wa_id(wa_id)
    user = get_or_create_by_identity(session, identity, profile_name)
    if (
        provider_message_id
        and session.execute(select(Message.id).where(Message.provider_message_id == provider_message_id)).first()
    ):
        return InboundResult(duplicate=True)
    now = utcnow()
    conv = get_active_session(session, user.id)
    conv.last_inbound_at = now
    reopen_window(session, user, now)  # 24h customer-service window starts again
    mtype = message_type or ("image" if images else "text")
    inbound = Message(
        session_id=conv.id,
        user_id=user.id,
        direction=MessageDirection.INBOUND,
        message_type=MessageType.IMAGE if mtype == "image" else MessageType.TEXT,
        provider_message_id=provider_message_id,
        body=(text or None) and text[:4000],
        media={"count": len(images)} if images else ({"type": mtype} if mtype != "text" else None),
    )
    session.add(inbound)
    session.flush()

    def done(replies: list[str], **kw: bool) -> InboundResult:
        _reply(session, conv, user.id, inbound, replies)
        return InboundResult(replies, **kw)

    if user.status != UserStatus.ACTIVE:
        return done([templates.ACCOUNT_SUSPENDED], blocked=True)

    if not get_rate_limiter().hit(f"user:{user.id}", get_settings().rate_limit_user_messages_per_minute, 60):
        record_security_event(
            session,
            user_id=user.id,
            event_type=SecurityEventType.RATE_LIMITED,
            severity=Severity.LOW,
            details={"scope": "user_messages"},
        )
        return InboundResult([], blocked=True)

    if mtype == "unsupported":
        return done([templates.UNSUPPORTED_MESSAGE])
    if mtype == "location":
        inbound.body = None
        return done([templates.LOCATION_NOT_ALLOWED])
    if media_failed:
        return done([templates.IMAGE_DOWNLOAD_FAILED])

    verdict = inspect_and_enforce(session, user.id, text, source="message", message_id=inbound.id)
    if not verdict.blocked and mtype == "contacts":
        # A contact card is contact data even when the detector finds no number in it.
        verdict = register_attempt(
            session,
            user.id,
            EvasionResult([Detection("CONTACT_CARD", "contacts message")]),
            source="message",
            message_id=inbound.id,
        )
    if verdict.blocked:
        inbound.blocked = True
        # No plaintext evidence is kept: encrypted for the admin who reviews the case.
        inbound.body_enc = seal_message_evidence(text) if text else None
        inbound.body = None
        return done([verdict.message or ""], blocked=True)

    if images:
        try:
            validate_images(images)
        except ImageRejected as e:
            return done([e.message])

    state = state_machine.reconcile(session, user, conv)
    intent = detect_intent(text, has_image=bool(images), state=state.value)
    conv.current_intent = intent.value

    if intent == Intent.NEXT_PAGE and not images and conv.search_session_id:
        page = search.next_page(session, user, conv.search_session_id)
        state_machine.transition(conv, state_machine.State.BROWSING_RESULTS, reason="next_page")
        return done([_format_page(page)])

    turn = run_turn(
        session,
        user,
        conv,
        text,
        client=client,
        gateway=gateway,
        storage=storage,
        images=images,
        image_mime=image_mime,
        inbound_message_id=inbound.id,
    )
    return done(turn.replies, used_dolphin=True)


def _format_page(page: search.SearchPage) -> str:
    if not page.results:
        return "No hay más resultados para esta búsqueda. Probá con otras palabras."
    lines = [
        f"{r['n']}. {r['title']} — {templates.money(r['price'], r['currency'])} — {r['zone']} ({r['item_id']})"
        for r in page.results
    ]
    tail = "" if page.exhausted else "\nEscribí 'más' para ver otros."
    return "\n".join(lines) + tail
