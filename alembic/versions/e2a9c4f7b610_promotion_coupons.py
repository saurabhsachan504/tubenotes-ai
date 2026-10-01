"""Add account-bound promotional checkout coupons.

Revision ID: e2a9c4f7b610
Revises: d9e4f6a8b203
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa


revision = "e2a9c4f7b610"
down_revision = "d9e4f6a8b203"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "promotion_coupons",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=48), nullable=False),
        sa.Column("offer_key", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("price_subunits", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("razorpay_plan_id", sa.String(length=128), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("provider_session_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_promotion_coupons_code"),
    )
    op.create_index("ix_promotion_coupons_user_id", "promotion_coupons", ["user_id"])
    op.create_index("ix_promotion_coupons_user_status", "promotion_coupons", ["user_id", "status"])
    op.create_index("ix_promotion_coupons_status_expires", "promotion_coupons", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_promotion_coupons_status_expires", table_name="promotion_coupons")
    op.drop_index("ix_promotion_coupons_user_status", table_name="promotion_coupons")
    op.drop_index("ix_promotion_coupons_user_id", table_name="promotion_coupons")
    op.drop_table("promotion_coupons")
