"""Add utc_offset_seconds to market_groups for local-city-time exits.

Captured from Open-Meteo (timezone="auto" → UtcOffsetSeconds()), DST-aware.
Used by local_now(group) to drive the time-gate / hard-floor tail exits.

Revision ID: 011
Revises: 010
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa

revision: str = "011"
down_revision: str = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        op.add_column(
            "market_groups",
            sa.Column("utc_offset_seconds", sa.Integer, nullable=True),
        )
    except Exception:
        pass  # idempotent — column already exists


def downgrade() -> None:
    # SQLite has no DROP COLUMN — skip
    pass
