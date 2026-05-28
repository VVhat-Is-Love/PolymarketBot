"""Add Polymarket matching keys to live_trades for PnL reconciliation

Adds: condition_id, token_id, basket_id, reconciled_with_polymarket

Revision ID: 009
Revises: 008
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa

revision: str = "009"
down_revision: str = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite does not support adding multiple columns in one ALTER TABLE,
    # so we add them one by one. Each wrapped in try/except for idempotency.
    for col_name, col_def in [
        ("condition_id", sa.Column("condition_id", sa.String, nullable=True)),
        ("token_id",     sa.Column("token_id",     sa.String, nullable=True)),
        ("basket_id",    sa.Column("basket_id",    sa.String, nullable=True)),
        ("reconciled_with_polymarket",
         sa.Column("reconciled_with_polymarket", sa.Boolean,
                   nullable=False, server_default="0")),
    ]:
        try:
            op.add_column("live_trades", col_def)
        except Exception:
            pass  # column already exists

    # Indexes for fast lookup
    try:
        op.create_index("ix_live_trades_condition_id", "live_trades", ["condition_id"])
    except Exception:
        pass
    try:
        op.create_index("ix_live_trades_basket_id", "live_trades", ["basket_id"])
    except Exception:
        pass


def downgrade() -> None:
    # SQLite does not support DROP COLUMN — skip
    pass
