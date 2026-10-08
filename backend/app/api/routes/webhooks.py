"""Webhook endpoints. Signature verified on the RAW body / headers before anything else; every delivery is
deduplicated in webhook_events (idempotency + replay protection)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api.deps import get_db, payment_gateway
from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.errors import ExternalServiceError
from app.models.enums import SecurityEventType, Severity
from app.models.payment import WebhookEvent
from app.services.mercadopago import signature as mp_sig
from app.services.payments.service import process_provider_notification
from app.services.security.enforcement import record_security_event
from app.services.whatsapp import signature as wa_sig
from app.services.whatsapp.parser import parse_webhook

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _claim(db: Session, provider: str, key: str, payload: dict) -> WebhookEvent | None:
    """Insert the ledger row; return it if this delivery must be processed, None if duplicate."""
    row = db.execute(
        insert(WebhookEvent)
        .values(provider=provider, dedupe_key=key[:200], payload=payload)
        .on_conflict_do_nothing(index_elements=["provider", "dedupe_key"])
        .returning(WebhookEvent.id)
    ).scalar_one_or_none()
    if row is not None:
        return db.get(WebhookEvent, row)
    existing = db.execute(
        select(WebhookEvent)
        .where(WebhookEvent.provider == provider, WebhookEvent.dedupe_key == key[:200])
        .with_for_update()
    ).scalar_one()
    return existing if existing.status == "FAILED" else None  # failed deliveries may be retried


# ---- WhatsApp ----------------------------------------------------------------------------------
@router.get("/whatsapp", response_class=PlainTextResponse)
def whatsapp_verify(
    mode: str | None = Query(None, alias="hub.mode"),
    token: str | None = Query(None, alias="hub.verify_token"),
    challenge: str | None = Query(None, alias="hub.challenge"),
) -> str:
    if not wa_sig.verify_handshake(mode, token, get_settings().whatsapp_verify_token.get_secret_value()):
        raise HTTPException(403, "verification failed")
    return challenge or ""


@router.post("/whatsapp")
async def whatsapp_receive(request: Request, db: Session = Depends(get_db)) -> dict:
    raw = await request.body()
    if len(raw) > 1_000_000:
        raise HTTPException(413, "payload too large")
    if not wa_sig.verify_signature(
        get_settings().whatsapp_app_secret.get_secret_value(), raw, request.headers.get("x-hub-signature-256")
    ):
        record_security_event(
            db,
            user_id=None,
            event_type=SecurityEventType.WEBHOOK_SIGNATURE_INVALID,
            severity=Severity.HIGH,
            details={"provider": "whatsapp"},
        )
        db.commit()
        raise HTTPException(401, "invalid signature")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid json") from None
    accepted = 0
    for msg in parse_webhook(payload):
        ev = _claim(
            db,
            "whatsapp",
            f"msg:{msg.message_id}",
            {"type": msg.type, "from": msg.wa_id, "ts": msg.timestamp, "media": list(msg.media_ids)},
        )
        if ev is not None:
            accepted += 1
    # PHASE 1: messages are durably queued (webhook_events, status RECEIVED). The phase 2 worker downloads
    # media, runs conversation.service.handle_inbound and sends replies from the outbox.
    return {"received": accepted}


# ---- Mercado Pago --------------------------------------------------------------------------------
@router.post("/mercadopago")
async def mercadopago_receive(
    request: Request, db: Session = Depends(get_db), gateway=Depends(payment_gateway)
) -> Response:
    s = get_settings()
    data_id = request.query_params.get("data.id") or request.query_params.get("id")
    topic = request.query_params.get("type") or request.query_params.get("topic") or ""
    request_id = request.headers.get("x-request-id")
    try:
        mp_sig.verify_signature(
            secret=s.mercadopago_webhook_secret.get_secret_value(),
            x_signature=request.headers.get("x-signature"),
            x_request_id=request_id,
            data_id=data_id,
            tolerance_seconds=s.mercadopago_signature_tolerance_seconds,
        )
    except mp_sig.SignatureError as e:
        replay = "tolerance" in str(e)
        record_security_event(
            db,
            user_id=None,
            event_type=SecurityEventType.WEBHOOK_REPLAY if replay else SecurityEventType.WEBHOOK_SIGNATURE_INVALID,
            severity=Severity.HIGH,
            details={"provider": "mercadopago", "reason": str(e)},
        )
        db.commit()
        raise HTTPException(401, "invalid signature") from None
    if not data_id:
        return Response(status_code=200)
    ev = _claim(db, "mercadopago", f"{topic}:{data_id}:{request_id or '-'}", {"type": topic, "data_id": data_id})
    if ev is None:
        return Response(status_code=200)  # duplicate / replayed delivery: already handled
    if topic != "payment":
        ev.status, ev.processed_at = "IGNORED", utcnow()
        return Response(status_code=200)
    if gateway is None:
        ev.status, ev.error = "FAILED", "gateway not configured"
        db.commit()
        raise HTTPException(503, "payments not configured")
    try:
        outcome = process_provider_notification(db, data_id, gateway)
    except ExternalServiceError:
        key = ev.dedupe_key
        db.rollback()  # nothing from this attempt is kept, except a FAILED ledger row so a retry reprocesses
        db.execute(
            insert(WebhookEvent)
            .values(
                provider="mercadopago",
                dedupe_key=key,
                status="FAILED",
                error="provider unavailable",
                payload={"data_id": data_id},
            )
            .on_conflict_do_update(
                index_elements=["provider", "dedupe_key"], set_={"status": "FAILED", "error": "provider unavailable"}
            )
        )
        db.commit()
        raise HTTPException(502, "provider unavailable, retry later") from None
    ev.status, ev.processed_at = "PROCESSED", utcnow()
    ev.payload = {**(ev.payload or {}), "outcome": outcome.value}
    return Response(status_code=200)
