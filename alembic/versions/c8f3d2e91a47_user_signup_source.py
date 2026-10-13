"""Track the client that created each TubeNotes account.

Revision ID: c8f3d2e91a47
Revises: c31f0a7b9d52
Create Date: 2026-10-13
"""
from alembic import op
import sqlalchemy as sa


revision = "c8f3d2e91a47"
down_revision = "c31f0a7b9d52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable keeps all existing accounts unchanged and represents their
    # unknown historical source truthfully. New accounts are web/extension.
    op.add_column("users", sa.Column("signup_source", sa.String(length=16), nullable=True))
    op.create_index("ix_users_signup_source", "users", ["signup_source"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_users_signup_source", table_name="users")
    op.drop_column("users", "signup_source")
