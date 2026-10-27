"""Add OAuth state for the TubeNotes MCP plugin.

Revision ID: f1a8c7d6e502
Revises: d2e7a3c9f410
Create Date: 2026-10-27
"""
from alembic import op
import sqlalchemy as sa


revision = "f1a8c7d6e502"
down_revision = "d2e7a3c9f410"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_oauth_clients",
        sa.Column("client_id", sa.String(length=2048), primary_key=True),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
    )
    op.create_table(
        "mcp_authorization_requests",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("client_id", sa.String(length=2048), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
        sa.Column("code_challenge", sa.String(length=256), nullable=False),
        sa.Column("scopes", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=2048)),
        sa.Column("resource", sa.String(length=2048)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_authorization_requests_client_id", "mcp_authorization_requests", ["client_id"])
    for table in ("mcp_authorization_codes", "mcp_access_tokens"):
        op.create_table(
            table,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("token_hash" if table == "mcp_access_tokens" else "code_hash", sa.String(length=64), unique=True, nullable=False),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("client_id", sa.String(length=2048), nullable=False),
            sa.Column("scopes", sa.String(length=512), nullable=False),
            sa.Column("resource", sa.String(length=2048)),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            *([] if table == "mcp_access_tokens" else [
                sa.Column("redirect_uri", sa.String(length=2048), nullable=False),
                sa.Column("code_challenge", sa.String(length=256), nullable=False),
                sa.Column("consumed_at", sa.DateTime(timezone=True)),
            ]),
            *([] if table == "mcp_authorization_codes" else [sa.Column("revoked_at", sa.DateTime(timezone=True))]),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
        op.create_index(f"ix_{table}_client_id", table, ["client_id"])


def downgrade() -> None:
    op.drop_table("mcp_access_tokens")
    op.drop_table("mcp_authorization_codes")
    op.drop_table("mcp_authorization_requests")
    op.drop_table("mcp_oauth_clients")
