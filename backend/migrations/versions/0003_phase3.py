"""phase 3: worker bookkeeping, outbox SENDING state, event handler retries, channel status, interactions

* job_state / job_runs / worker_heartbeats: recoverable scheduler (abandoned runs detected at restart).
* notification_status += SENDING and notifications.sending_started_at: a notification is claimed (committed)
  before the WhatsApp call, so a crash mid-send is recovered through the status webhook
  (biz_opaque_callback_data) instead of resending blindly.
* domain_events.failed_handlers / attempts / last_error: best-effort event handlers are retried.
* items.channel_status (separate from the publication status), score_breakdown, score_updated_at;
  backfilled from channel_posts.
* item_interactions: distinct-user daily counters for the attractiveness score.
* payments.checkout_expires_at (backfilled from the transaction's payment deadline).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_RUN_STATUS = postgresql.ENUM(
    "RUNNING", "SUCCEEDED", "FAILED", "SKIPPED", "ABANDONED", name="job_run_status", create_type=False
)
INTERACTION_KIND = postgresql.ENUM(
    "IMPRESSION", "SELECTION", "VIEW", "OFFER", name="interaction_kind", create_type=False
)
ITEM_CHANNEL_STATUS = postgresql.ENUM(
    "NOT_POSTED", "QUEUED", "VISIBLE", "REMOVAL_PENDING", "REMOVED", name="item_channel_status", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("ALTER TYPE notification_status ADD VALUE IF NOT EXISTS 'SENDING'")
    for enum in (JOB_RUN_STATUS, INTERACTION_KIND, ITEM_CHANNEL_STATUS):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "job_runs",
        sa.Column("job_name", sa.String(length=60), nullable=False),
        sa.Column("worker_id", sa.String(length=80), nullable=True),
        sa.Column("status", JOB_RUN_STATUS, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("affected", sa.Integer(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_runs")),
    )
    op.create_index("ix_job_runs_job_started", "job_runs", ["job_name", "started_at"], unique=False)
    op.create_table(
        "job_state",
        sa.Column("job_name", sa.String(length=60), nullable=False),
        sa.Column("status", JOB_RUN_STATUS, nullable=False),
        sa.Column("worker_id", sa.String(length=80), nullable=True),
        sa.Column("current_run_id", sa.BigInteger(), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("last_affected", sa.Integer(), nullable=True),
        sa.Column("last_duration_ms", sa.Integer(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("runs", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("failures", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("job_name", name=op.f("pk_job_state")),
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=80), nullable=False),
        sa.Column("hostname", sa.String(length=120), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("info", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.PrimaryKeyConstraint("worker_id", name=op.f("pk_worker_heartbeats")),
    )
    op.create_table(
        "item_interactions",
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", INTERACTION_KIND, nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("count", sa.Integer(), server_default="1", nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_item_interactions_item_id_items"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_item_interactions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("item_id", "user_id", "kind", "day", name=op.f("pk_item_interactions")),
    )
    op.create_index("ix_item_interactions_day", "item_interactions", ["day"], unique=False)

    op.add_column(
        "domain_events",
        sa.Column("failed_handlers", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
    )
    op.add_column("domain_events", sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("domain_events", sa.Column("last_error", sa.String(length=500), nullable=True))
    # Phase-1/2 events were dispatched synchronously and never retried: mark them processed.
    bind.execute(sa.text("UPDATE domain_events SET processed_at = created_at WHERE processed_at IS NULL"))
    op.create_index(
        "ix_domain_events_unprocessed",
        "domain_events",
        ["id"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL"),
    )

    op.add_column(
        "items", sa.Column("channel_status", ITEM_CHANNEL_STATUS, server_default="NOT_POSTED", nullable=False)
    )
    op.add_column("items", sa.Column("score_breakdown", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("items", sa.Column("score_updated_at", sa.DateTime(timezone=True), nullable=True))
    bind.execute(
        sa.text(
            """
            UPDATE items i SET channel_status = CASE
                WHEN EXISTS (SELECT 1 FROM channel_posts p WHERE p.item_id = i.id AND p.status = 'REMOVAL_PENDING')
                    THEN 'REMOVAL_PENDING'
                WHEN EXISTS (SELECT 1 FROM channel_posts p WHERE p.item_id = i.id AND p.status = 'PUBLISHED')
                    THEN 'VISIBLE'
                WHEN EXISTS (SELECT 1 FROM channel_posts p WHERE p.item_id = i.id
                             AND p.status IN ('PENDING', 'SENDING', 'RETRY_PENDING'))
                    THEN 'QUEUED'
                WHEN EXISTS (SELECT 1 FROM channel_posts p WHERE p.item_id = i.id AND p.status = 'REMOVED'
                             AND p.posted_at IS NOT NULL)
                    THEN 'REMOVED'
                ELSE 'NOT_POSTED' END::item_channel_status
            """
        )
    )

    op.add_column("notifications", sa.Column("sending_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("checkout_expires_at", sa.DateTime(timezone=True), nullable=True))
    bind.execute(
        sa.text(
            "UPDATE payments p SET checkout_expires_at = t.payment_due_at FROM transactions t "
            "WHERE t.id = p.transaction_id AND p.checkout_url IS NOT NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_column("payments", "checkout_expires_at")
    op.drop_column("notifications", "sending_started_at")
    # notification_status: drop SENDING (PostgreSQL cannot remove enum values -> recreate the type).
    # An in-flight SENDING row goes back to PENDING (at-least-once, as in phase 2).
    bind.execute(sa.text("UPDATE notifications SET status='PENDING' WHERE status::text='SENDING'"))
    op.drop_index("ix_notifications_status_created", table_name="notifications")
    op.drop_index("ix_notifications_dispatch", table_name="notifications")
    op.execute("ALTER TABLE notifications ALTER COLUMN status DROP DEFAULT")
    op.execute("ALTER TYPE notification_status RENAME TO notification_status_v3")
    op.execute("CREATE TYPE notification_status AS ENUM ('PENDING', 'SENT', 'FAILED', 'AWAITING_WINDOW', 'EXPIRED')")
    op.execute(
        "ALTER TABLE notifications ALTER COLUMN status TYPE notification_status USING status::text::notification_status"
    )
    op.execute("ALTER TABLE notifications ALTER COLUMN status SET DEFAULT 'PENDING'")
    op.execute("DROP TYPE notification_status_v3")
    op.create_index("ix_notifications_status_created", "notifications", ["status", "created_at"], unique=False)
    op.create_index("ix_notifications_dispatch", "notifications", ["status", "next_attempt_at"], unique=False)

    op.drop_column("items", "score_updated_at")
    op.drop_column("items", "score_breakdown")
    op.drop_column("items", "channel_status")
    op.drop_index(
        "ix_domain_events_unprocessed", table_name="domain_events", postgresql_where=sa.text("processed_at IS NULL")
    )
    op.drop_column("domain_events", "last_error")
    op.drop_column("domain_events", "attempts")
    op.drop_column("domain_events", "failed_handlers")
    op.drop_index("ix_item_interactions_day", table_name="item_interactions")
    op.drop_table("item_interactions")
    op.drop_table("worker_heartbeats")
    op.drop_table("job_state")
    op.drop_index("ix_job_runs_job_started", table_name="job_runs")
    op.drop_table("job_runs")
    for enum in (ITEM_CHANNEL_STATUS, INTERACTION_KIND, JOB_RUN_STATUS):
        enum.drop(bind, checkfirst=True)
