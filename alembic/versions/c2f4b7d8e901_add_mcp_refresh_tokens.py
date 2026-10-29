"""Persist and rotate OAuth refresh tokens for ChatGPT MCP connections.

Revision ID: c2f4b7d8e901
Revises: bc7e4f1a2d53
Create Date: 2026-10-29
"""
from alembic import op
import sqlalchemy as sa


revision = "c2f4b7d8e901"
down_revision = "bc7e4f1a2d53"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_refresh_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.String(length=2048), nullable=False),
        sa.Column("scopes", sa.String(length=512), nullable=False),
        sa.Column("resource", sa.String(length=2048)),
        sa.Column("machine_hash", sa.String(length=64)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_mcp_refresh_tokens_user_id", "mcp_refresh_tokens", ["user_id"])
    op.create_index("ix_mcp_refresh_tokens_client_id", "mcp_refresh_tokens", ["client_id"])
    op.create_index("ix_mcp_refresh_tokens_machine_hash", "mcp_refresh_tokens", ["machine_hash"])
    op.add_column("mcp_access_tokens", sa.Column("refresh_token_id", sa.String(length=36), nullable=True))
    op.create_foreign_key(
        "fk_mcp_access_tokens_refresh_token_id",
        "mcp_access_tokens",
        "mcp_refresh_tokens",
        ["refresh_token_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_mcp_access_tokens_refresh_token_id", "mcp_access_tokens", ["refresh_token_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_access_tokens_refresh_token_id", table_name="mcp_access_tokens")
    op.drop_constraint("fk_mcp_access_tokens_refresh_token_id", "mcp_access_tokens", type_="foreignkey")
    op.drop_column("mcp_access_tokens", "refresh_token_id")
    op.drop_index("ix_mcp_refresh_tokens_machine_hash", table_name="mcp_refresh_tokens")
    op.drop_index("ix_mcp_refresh_tokens_client_id", table_name="mcp_refresh_tokens")
    op.drop_index("ix_mcp_refresh_tokens_user_id", table_name="mcp_refresh_tokens")
    op.drop_table("mcp_refresh_tokens")
