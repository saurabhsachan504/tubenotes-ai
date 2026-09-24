"""Store an account's immutable billing-country pricing tier.

Revision ID: f2c8b4d91e30
Revises: e5b3f1a77c22
Create Date: 2026-09-21
"""
from alembic import op
import sqlalchemy as sa


revision = "f2c8b4d91e30"
down_revision = "e5b3f1a77c22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("billing_country", sa.String(length=8), nullable=True))
    op.create_index("ix_users_billing_country", "users", ["billing_country"])


def downgrade() -> None:
    op.drop_index("ix_users_billing_country", table_name="users")
    op.drop_column("users", "billing_country")
