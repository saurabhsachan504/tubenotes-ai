"""Track whether a processing job came from web or Chrome extension.

Revision ID: f6c2a1e9d705
Revises: e2a9c4f7b610
Create Date: 2026-10-01
"""
from alembic import op
import sqlalchemy as sa


revision = "f6c2a1e9d705"
down_revision = "e2a9c4f7b610"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A server default preserves every historical job as web activity. New
    # requests explicitly record their source in the audit writer.
    op.add_column(
        "processing_jobs",
        sa.Column("client_source", sa.String(length=20), nullable=False, server_default="web"),
    )
    op.create_index(
        "ix_processing_jobs_source_started", "processing_jobs", ["client_source", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_processing_jobs_source_started", table_name="processing_jobs")
    op.drop_column("processing_jobs", "client_source")
