"""short-lived per-user transcript cache for MCP PDF reuse

Revision ID: ab3f2e8d1c91
Revises: f1a8c7d6e502
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa


revision = "ab3f2e8d1c91"
down_revision = "f1a8c7d6e502"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_transcript_cache",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("video_id", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="server"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "video_id", name="uq_mcp_transcript_user_video"),
    )
    op.create_index("ix_mcp_transcript_cache_expires_at", "mcp_transcript_cache", ["expires_at"])
    op.create_index("ix_mcp_transcript_cache_user_id", "mcp_transcript_cache", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_transcript_cache_user_id", table_name="mcp_transcript_cache")
    op.drop_index("ix_mcp_transcript_cache_expires_at", table_name="mcp_transcript_cache")
    op.drop_table("mcp_transcript_cache")
