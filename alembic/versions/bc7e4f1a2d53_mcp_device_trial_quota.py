"""bind MCP tokens to a browser machine and add the MCP-only trial ledger

Revision ID: bc7e4f1a2d53
Revises: ab3f2e8d1c91
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa


revision = "bc7e4f1a2d53"
down_revision = "ab3f2e8d1c91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("mcp_authorization_codes", sa.Column("machine_hash", sa.String(length=64), nullable=True))
    op.add_column("mcp_access_tokens", sa.Column("machine_hash", sa.String(length=64), nullable=True))
    op.create_index("ix_mcp_access_tokens_machine_hash", "mcp_access_tokens", ["machine_hash"])
    op.create_table(
        "mcp_trial_ledger",
        sa.Column("machine_hash", sa.String(length=64), nullable=False),
        sa.Column("trials_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("machine_hash"),
    )
    op.create_table(
        "mcp_trial_usages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("machine_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("video_id", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["machine_hash"], ["mcp_trial_ledger.machine_hash"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("machine_hash", "video_id", name="uq_mcp_trial_machine_video"),
    )
    op.create_index("ix_mcp_trial_usage_user_id", "mcp_trial_usages", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_trial_usage_user_id", table_name="mcp_trial_usages")
    op.drop_table("mcp_trial_usages")
    op.drop_table("mcp_trial_ledger")
    op.drop_index("ix_mcp_access_tokens_machine_hash", table_name="mcp_access_tokens")
    op.drop_column("mcp_access_tokens", "machine_hash")
    op.drop_column("mcp_authorization_codes", "machine_hash")
