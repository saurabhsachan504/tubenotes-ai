"""Store the location snapshot associated with each processing job.

Revision ID: b3d5e7f9a102
Revises: a7d9e2c41b55
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa


revision = "b3d5e7f9a102"
down_revision = "a7d9e2c41b55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("processing_jobs")}
    if "request_city" not in columns:
        op.add_column(
            "processing_jobs",
            sa.Column("request_city", sa.String(length=120), nullable=False, server_default="Unknown"),
        )
    if "request_country" not in columns:
        op.add_column(
            "processing_jobs",
            sa.Column("request_country", sa.String(length=8), nullable=False, server_default="Unknown"),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("processing_jobs")}
    if "request_country" in columns:
        op.drop_column("processing_jobs", "request_country")
    if "request_city" in columns:
        op.drop_column("processing_jobs", "request_city")
