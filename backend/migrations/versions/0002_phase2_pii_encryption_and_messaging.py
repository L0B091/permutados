"""phase 2: field-level PII encryption, WhatsApp queue/outbox columns

* users.whatsapp_id / phone / display_name (plaintext) -> whatsapp_hash (keyed blind index), whatsapp_enc and
  display_name_enc (AES-256-GCM, app.core.crypto, token format v1), whatsapp_last4; last_inbound_at and
  last_template_at for the 24h-window logic. Existing rows are encrypted in place with ENCRYPTION_KEY.
* messages.body_enc: blocked (anti-evasion) messages keep only encrypted evidence.
* notifications: encrypted body for sensitive messages, retry/expiry/channel columns, new statuses
  AWAITING_WINDOW and EXPIRED.
* webhook_events: attempts / next_attempt_at for the inbound queue.

Downgrade decrypts back to the phase-1 plaintext columns (requires the same ENCRYPTION_KEY).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SENSITIVE_PLACEHOLDER = "[mensaje cifrado: contiene datos de contacto]"
PHASE1_TEMPLATE_NAMES = ("offer_received", "offer_accepted", "contact_released", "publication_expiring")


def _cipher():
    from app.core import crypto

    crypto.get_cipher.cache_clear()
    return crypto, crypto.get_cipher()


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("ALTER TYPE notification_status ADD VALUE IF NOT EXISTS 'AWAITING_WINDOW'")
    op.execute("ALTER TYPE notification_status ADD VALUE IF NOT EXISTS 'EXPIRED'")

    op.add_column("messages", sa.Column("body_enc", sa.Text(), nullable=True))
    op.add_column("notifications", sa.Column("body_enc", sa.Text(), nullable=True))
    op.add_column("notifications", sa.Column("sensitive", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("notifications", sa.Column("summary", sa.String(length=200), nullable=True))
    op.add_column("notifications", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("notifications", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("notifications", sa.Column("channel", sa.String(length=20), nullable=True))
    op.create_index("ix_notifications_dispatch", "notifications", ["status", "next_attempt_at"], unique=False)
    op.add_column("users", sa.Column("whatsapp_hash", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("whatsapp_enc", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("whatsapp_last4", sa.String(length=4), nullable=True))
    op.add_column("users", sa.Column("display_name_enc", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_template_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("webhook_events", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("webhook_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_webhook_events_queue", "webhook_events", ["provider", "status", "id"], unique=False)

    # ---- data migration ------------------------------------------------------------------------
    users = bind.execute(sa.text("SELECT id, whatsapp_id, display_name FROM users")).all()
    blocked = bind.execute(sa.text("SELECT id, body FROM messages WHERE blocked AND body IS NOT NULL")).all()
    sensitive = bind.execute(sa.text("SELECT id, body FROM notifications WHERE kind LIKE 'CONTACT_RELEASED_%'")).all()
    if users or blocked or sensitive:
        crypto, c = _cipher()  # raises a clear error if ENCRYPTION_KEY is missing/invalid
        for uid, wa, name in users:
            bind.execute(
                sa.text(
                    "UPDATE users SET whatsapp_hash=:h, whatsapp_enc=:e, whatsapp_last4=:l, display_name_enc=:n "
                    "WHERE id=:id"
                ),
                {
                    "h": c.blind_index(wa, crypto.WA_ID),
                    "e": c.encrypt(wa, crypto.WA_ID),
                    "l": wa[-4:],
                    "n": c.encrypt(name, crypto.DISPLAY_NAME) if name else None,
                    "id": uid,
                },
            )
        for mid, body in blocked:
            bind.execute(
                sa.text("UPDATE messages SET body_enc=:e, body=NULL WHERE id=:id"),
                {"e": c.encrypt(body, crypto.MESSAGE_EVIDENCE), "id": mid},
            )
        for nid, body in sensitive:
            bind.execute(
                sa.text("UPDATE notifications SET body_enc=:e, body=:p, sensitive=true WHERE id=:id"),
                {"e": c.encrypt(body, crypto.NOTIFICATION_BODY), "p": SENSITIVE_PLACEHOLDER, "id": nid},
            )
    bind.execute(
        sa.text(
            "UPDATE users u SET last_inbound_at = s.last FROM (SELECT user_id, max(last_inbound_at) AS last "
            "FROM conversation_sessions GROUP BY user_id) s WHERE s.user_id = u.id"
        )
    )
    bind.execute(
        sa.text("UPDATE notifications SET template_name = NULL WHERE template_name = ANY(:names)"),
        {"names": list(PHASE1_TEMPLATE_NAMES)},
    )
    # Phase-1 queue rows did not keep the message text, they cannot be processed by the new worker.
    bind.execute(
        sa.text(
            "UPDATE webhook_events SET status='IGNORED', error='phase1_payload_without_text', processed_at=now() "
            "WHERE provider='whatsapp' AND status='RECEIVED'"
        )
    )

    op.alter_column("users", "whatsapp_hash", nullable=False)
    op.alter_column("users", "whatsapp_enc", nullable=False)
    op.alter_column("users", "whatsapp_last4", nullable=False)
    op.create_unique_constraint(op.f("uq_users_whatsapp_hash"), "users", ["whatsapp_hash"])
    op.create_check_constraint(op.f("ck_users_whatsapp_hash_hex"), "users", "whatsapp_hash ~ '^[0-9a-f]{64}$'")
    op.create_check_constraint(op.f("ck_users_whatsapp_last4_digits"), "users", "whatsapp_last4 ~ '^[0-9]{4}$'")
    op.drop_constraint(op.f("ck_users_whatsapp_id_digits"), "users", type_="check")
    op.drop_constraint(op.f("uq_users_whatsapp_id"), "users", type_="unique")
    op.drop_column("users", "whatsapp_id")
    op.drop_column("users", "phone")
    op.drop_column("users", "display_name")


def downgrade() -> None:
    bind = op.get_bind()
    op.add_column("users", sa.Column("display_name", sa.VARCHAR(length=120), nullable=True))
    op.add_column("users", sa.Column("phone", sa.VARCHAR(length=20), nullable=True))
    op.add_column("users", sa.Column("whatsapp_id", sa.VARCHAR(length=20), nullable=True))

    users = bind.execute(sa.text("SELECT id, whatsapp_enc, display_name_enc FROM users")).all()
    blocked = bind.execute(sa.text("SELECT id, body_enc FROM messages WHERE body_enc IS NOT NULL")).all()
    sensitive = bind.execute(sa.text("SELECT id, body_enc FROM notifications WHERE sensitive")).all()
    if users or blocked or sensitive:
        crypto, c = _cipher()
        for uid, enc, name_enc in users:
            wa = c.decrypt(enc, crypto.WA_ID)
            bind.execute(
                sa.text("UPDATE users SET whatsapp_id=:w, phone=:w, display_name=:n WHERE id=:id"),
                {"w": wa, "n": c.decrypt(name_enc, crypto.DISPLAY_NAME) if name_enc else None, "id": uid},
            )
        for mid, enc in blocked:
            bind.execute(
                sa.text("UPDATE messages SET body=:b WHERE id=:id"),
                {"b": c.decrypt(enc, crypto.MESSAGE_EVIDENCE), "id": mid},
            )
        for nid, enc in sensitive:
            if enc:
                bind.execute(
                    sa.text("UPDATE notifications SET body=:b WHERE id=:id"),
                    {"b": c.decrypt(enc, crypto.NOTIFICATION_BODY), "id": nid},
                )

    op.alter_column("users", "whatsapp_id", nullable=False)
    op.drop_constraint(op.f("ck_users_whatsapp_last4_digits"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_whatsapp_hash_hex"), "users", type_="check")
    op.drop_constraint(op.f("uq_users_whatsapp_hash"), "users", type_="unique")
    op.create_unique_constraint(op.f("uq_users_whatsapp_id"), "users", ["whatsapp_id"])
    op.create_check_constraint(op.f("ck_users_whatsapp_id_digits"), "users", "whatsapp_id ~ '^[0-9]{6,20}$'")
    op.drop_column("users", "last_template_at")
    op.drop_column("users", "last_inbound_at")
    op.drop_column("users", "display_name_enc")
    op.drop_column("users", "whatsapp_last4")
    op.drop_column("users", "whatsapp_enc")
    op.drop_column("users", "whatsapp_hash")

    op.drop_index("ix_webhook_events_queue", table_name="webhook_events")
    op.drop_column("webhook_events", "next_attempt_at")
    op.drop_column("webhook_events", "attempts")

    # notification_status: remove the two values (PostgreSQL cannot drop enum values -> recreate the type).
    bind.execute(sa.text("UPDATE notifications SET status='PENDING' WHERE status::text='AWAITING_WINDOW'"))
    bind.execute(sa.text("UPDATE notifications SET status='FAILED' WHERE status::text='EXPIRED'"))
    op.drop_index("ix_notifications_dispatch", table_name="notifications")
    op.drop_index("ix_notifications_status_created", table_name="notifications")
    op.execute("ALTER TABLE notifications ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE notification_status RENAME TO notification_status_v2")
    op.execute("CREATE TYPE notification_status AS ENUM ('PENDING', 'SENT', 'FAILED')")
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN status TYPE notification_status USING status::text::notification_status"
    )
    op.execute("ALTER TABLE notifications ALTER COLUMN status SET DEFAULT 'PENDING'")
    op.execute("DROP TYPE notification_status_v2")
    op.create_index("ix_notifications_status_created", "notifications", ["status", "created_at"], unique=False)
    for col in ("channel", "expires_at", "next_attempt_at", "summary", "sensitive", "body_enc"):
        op.drop_column("notifications", col)
    op.drop_column("messages", "body_enc")
