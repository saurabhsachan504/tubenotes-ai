"""Record one-time transactional notification claims.

Revision ID: f8d3a7c4e219
Revises: f6c2a1e9d705
Create Date: 2026-10-07
"""
from alembic import op
import sqlalchemy as sa


revision = "f8d3a7c4e219"
down_revision = "f6c2a1e9d705"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_notifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "kind", name="uq_email_notifications_user_kind"),
    )
    op.create_index(
        "ix_email_notifications_user_id", "email_notifications", ["user_id"]
    )
    op.create_index(
        "ix_email_notifications_user_kind",
        "email_notifications",
        ["user_id", "kind"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_notifications_user_kind", table_name="email_notifications")
    op.drop_index("ix_email_notifications_user_id", table_name="email_notifications")
    op.drop_table("email_notifications")
