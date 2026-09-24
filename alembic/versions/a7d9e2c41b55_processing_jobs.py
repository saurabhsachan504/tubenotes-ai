"""Add operational processing job audit records for the admin dashboard.

Revision ID: a7d9e2c41b55
Revises: f2c8b4d91e30
Create Date: 2026-09-22
"""
from alembic import op
import sqlalchemy as sa


revision = "a7d9e2c41b55"
down_revision = "f2c8b4d91e30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # In local dev ``init_db()`` uses metadata.create_all() at startup.  If a
    # developer starts the app before running Alembic, the table already
    # exists but the revision is not stamped yet.  Treat that safe state as an
    # applied schema rather than failing the otherwise normal migration.
    if "processing_jobs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("video_id", sa.String(length=16), nullable=True),
        sa.Column("video_url", sa.String(length=500), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="processing"),
        sa.Column("pdf_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("output_chars", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_processing_jobs_started_at", "processing_jobs", ["started_at"])
    op.create_index("ix_processing_jobs_user_started", "processing_jobs", ["user_id", "started_at"])
    op.create_index("ix_processing_jobs_status_started", "processing_jobs", ["status", "started_at"])
    op.create_index("ix_processing_jobs_video_id", "processing_jobs", ["video_id"])


def downgrade() -> None:
    if "processing_jobs" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_processing_jobs_video_id", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_status_started", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_user_started", table_name="processing_jobs")
    op.drop_index("ix_processing_jobs_started_at", table_name="processing_jobs")
    op.drop_table("processing_jobs")
