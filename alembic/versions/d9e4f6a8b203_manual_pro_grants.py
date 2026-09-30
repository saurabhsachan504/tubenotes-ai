"""Add auditable manual complimentary-Pro grants.

Revision ID: d9e4f6a8b203
Revises: b14f7c8d9e01
Create Date: 2026-09-30
"""
from alembic import op
import sqlalchemy as sa


revision = "d9e4f6a8b203"
down_revision = "b14f7c8d9e01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "manual_pro_grants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("granted_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("granted_by_email", sa.String(length=320), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("revoked_by_email", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_manual_pro_grants_user_id", "manual_pro_grants", ["user_id"])
    op.create_index("ix_manual_pro_grants_user_status", "manual_pro_grants", ["user_id", "status"])
    op.create_index("ix_manual_pro_grants_active_expires", "manual_pro_grants", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_manual_pro_grants_active_expires", table_name="manual_pro_grants")
    op.drop_index("ix_manual_pro_grants_user_status", table_name="manual_pro_grants")
    op.drop_index("ix_manual_pro_grants_user_id", table_name="manual_pro_grants")
    op.drop_table("manual_pro_grants")
