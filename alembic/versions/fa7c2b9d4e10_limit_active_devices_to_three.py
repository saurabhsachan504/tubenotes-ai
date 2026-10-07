"""Keep only the three most recently active devices per account.

Revision ID: fa7c2b9d4e10
Revises: f8d3a7c4e219
Create Date: 2026-10-07
"""
from collections import defaultdict

from alembic import op
import sqlalchemy as sa


revision = "fa7c2b9d4e10"
down_revision = "f8d3a7c4e219"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Revoke surplus historical devices, retaining each user's newest three.

    Device rows are deliberately retained for audit history. Revocation is
    reversible by the account holder: removing an active device frees a slot,
    then signing in again from an older device activates it again.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT user_id, id FROM devices "
            "WHERE revoked = :active "
            "ORDER BY user_id, last_seen_at DESC, created_at DESC, id DESC"
        ),
        {"active": False},
    ).mappings()

    devices_by_user: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        devices_by_user[row["user_id"]].append(row["id"])

    surplus_ids = [
        device_id
        for device_ids in devices_by_user.values()
        for device_id in device_ids[3:]
    ]
    if not surplus_ids:
        return

    statement = sa.text(
        "UPDATE devices SET revoked = :revoked WHERE id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    bind.execute(statement, {"revoked": True, "ids": surplus_ids})

    # Existing refresh tokens on removed devices must not remain renewable.
    bind.execute(
        sa.text(
            "UPDATE refresh_tokens SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE device_hash IN (SELECT device_hash FROM devices WHERE id IN :ids) "
            "AND revoked_at IS NULL"
        ).bindparams(sa.bindparam("ids", expanding=True)),
        {"ids": surplus_ids},
    )


def downgrade() -> None:
    # We cannot know whether a row was revoked before this one-time cleanup, so
    # do not reactivate any device automatically on downgrade.
    pass
