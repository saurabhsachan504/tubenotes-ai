"""Add browser Web Push subscriptions.

Revision ID: fb8c1d2e9a04
Revises: fa7c2b9d4e10
Create Date: 2026-10-08
"""
from alembic import op
import sqlalchemy as sa

revision = "fb8c1d2e9a04"
down_revision = "fa7c2b9d4e10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "web_push_subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.String(length=255), nullable=False),
        sa.Column("auth", sa.String(length=255), nullable=False),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint", name="uq_web_push_subscriptions_endpoint"),
    )
    op.create_index("ix_web_push_subscriptions_user_id", "web_push_subscriptions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_web_push_subscriptions_user_id", table_name="web_push_subscriptions")
    op.drop_table("web_push_subscriptions")
