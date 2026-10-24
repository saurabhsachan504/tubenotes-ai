"""Remove discontinued newsletter feature.

Revision ID: d2e7a3c9f410
Revises: c4a8e9b0d315
Create Date: 2026-10-24
"""
from alembic import op
import sqlalchemy as sa


revision = "d2e7a3c9f410"
down_revision = "c4a8e9b0d315"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("newsletter_runs")
    op.drop_column("users", "newsletter_unsubscribed_at")
    op.drop_column("users", "newsletter_opted_in_at")
    op.drop_column("users", "newsletter_opt_in")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("newsletter_opt_in", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("users", sa.Column("newsletter_opted_in_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("newsletter_unsubscribed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "newsletter_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("schedule_key", sa.String(length=48), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("recipient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delivered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("schedule_key", name="uq_newsletter_runs_schedule_key"),
    )
