"""add browser push campaigns

Revision ID: c31f0a7b9d52
Revises: fb8c1d2e9a04
Create Date: 2026-10-09
"""
from alembic import op
import sqlalchemy as sa


revision = "c31f0a7b9d52"
down_revision = "fb8c1d2e9a04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_campaigns",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("daily_time", sa.String(length=5), nullable=False),
        sa.Column("weekly_day", sa.Integer(), nullable=False),
        sa.Column("cooldown_hours", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", name="uq_push_campaign_kind"),
    )
    op.create_table(
        "push_campaign_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("trigger_key", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_users", sa.Integer(), nullable=False),
        sa.Column("target_endpoints", sa.Integer(), nullable=False),
        sa.Column("sent_endpoints", sa.Integer(), nullable=False),
        sa.Column("failed_endpoints", sa.Integer(), nullable=False),
        sa.Column("stale_endpoints", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["push_campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "trigger_key", name="uq_push_campaign_run_key"),
    )
    op.create_index("ix_push_campaign_runs_campaign_id", "push_campaign_runs", ["campaign_id"])
    op.create_index("ix_push_campaign_runs_campaign_started", "push_campaign_runs", ["campaign_id", "started_at"])
    op.create_table(
        "push_campaign_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("endpoints_sent", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["push_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "user_id", name="uq_push_campaign_delivery_user"),
    )
    op.create_index("ix_push_campaign_deliveries_campaign_id", "push_campaign_deliveries", ["campaign_id"])
    op.create_index("ix_push_campaign_deliveries_user_id", "push_campaign_deliveries", ["user_id"])
    op.create_index("ix_push_campaign_deliveries_campaign_sent", "push_campaign_deliveries", ["campaign_id", "last_sent_at"])


def downgrade() -> None:
    op.drop_index("ix_push_campaign_deliveries_campaign_sent", table_name="push_campaign_deliveries")
    op.drop_index("ix_push_campaign_deliveries_user_id", table_name="push_campaign_deliveries")
    op.drop_index("ix_push_campaign_deliveries_campaign_id", table_name="push_campaign_deliveries")
    op.drop_table("push_campaign_deliveries")
    op.drop_index("ix_push_campaign_runs_campaign_started", table_name="push_campaign_runs")
    op.drop_index("ix_push_campaign_runs_campaign_id", table_name="push_campaign_runs")
    op.drop_table("push_campaign_runs")
    op.drop_table("push_campaigns")
