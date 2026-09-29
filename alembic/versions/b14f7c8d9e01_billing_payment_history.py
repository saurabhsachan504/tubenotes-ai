"""Store verified customer payment history and invoice links.

Revision ID: b14f7c8d9e01
Revises: b3d5e7f9a102
Create Date: 2026-09-29
"""
from alembic import op
import sqlalchemy as sa


revision = "b14f7c8d9e01"
down_revision = "b3d5e7f9a102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "billing_payments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=128), nullable=True),
        sa.Column("provider_subscription_id", sa.String(length=128), nullable=True),
        sa.Column("provider_invoice_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("amount_subunits", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("invoice_url", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_billing_payment_provider_id"),
    )
    op.create_index("ix_billing_payments_user_id", "billing_payments", ["user_id"])
    op.create_index("ix_billing_payments_provider_subscription_id", "billing_payments", ["provider_subscription_id"])
    op.create_index("ix_billing_payments_provider_invoice_id", "billing_payments", ["provider_invoice_id"])
    op.create_index("ix_billing_payments_user_created", "billing_payments", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_billing_payments_user_created", table_name="billing_payments")
    op.drop_index("ix_billing_payments_provider_invoice_id", table_name="billing_payments")
    op.drop_index("ix_billing_payments_provider_subscription_id", table_name="billing_payments")
    op.drop_index("ix_billing_payments_user_id", table_name="billing_payments")
    op.drop_table("billing_payments")
