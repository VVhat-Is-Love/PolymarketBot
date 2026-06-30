"""Add fill_notified flag to live_trades (one FILLED alert per position)

Adds: fill_notified — set True after the first "✅ Исполнен" notification so a
position that reconcile re-verifies every cycle is never re-announced.

Revision ID: 010
Revises: 009
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision: str = "010"
down_revision: str = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        op.add_column(
            "live_trades",
            sa.Column("fill_notified", sa.Boolean, nullable=False, server_default="0"),
        )
    except Exception:
        pass  # column already exists (idempotent)


def downgrade() -> None:
    # SQLite does not support DROP COLUMN — skip
    pass
