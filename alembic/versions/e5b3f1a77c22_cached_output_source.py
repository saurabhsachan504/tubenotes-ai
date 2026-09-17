"""cached_outputs.source - where the transcript behind a row came from

Revision ID: e5b3f1a77c22
Revises: d4c81a6f22b7
Create Date: 2026-09-17

Results built from an extension-supplied transcript are now cacheable when
oEmbed confirms the video needs no login. That text is unverified, so every row
records its origin and a suspect batch can be purged without touching rows the
server fetched itself. Existing rows predate the change and were all
server-fetched, hence the "server" default.
"""
from alembic import op
import sqlalchemy as sa

revision = "e5b3f1a77c22"
down_revision = "d4c81a6f22b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cached_outputs",
        sa.Column("source", sa.String(16), nullable=False, server_default="server"),
    )


def downgrade() -> None:
    op.drop_column("cached_outputs", "source")
