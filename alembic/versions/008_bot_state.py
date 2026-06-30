"""bot_state persistent key-value table

Revision ID: 008
Revises: 007
Create Date: 2026-05-28

"""
from alembic import op
import sqlalchemy as sa

revision: str = "008"
down_revision: str = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_state",
        sa.Column("key", sa.String, primary_key=True, nullable=False),
        sa.Column("value", sa.String, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )
    # Add strategy_name column to live_trades (missing from migration 007)
    try:
        op.add_column("live_trades", sa.Column(
            "strategy_name", sa.String(16), server_default="basket_wide", nullable=True
        ))
    except Exception:
        pass  # column already exists


def downgrade() -> None:
    op.drop_table("bot_state")
