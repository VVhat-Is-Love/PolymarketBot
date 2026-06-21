"""Add sell_order_id and sell_placed_at to live_trades (T3 partial-exit tracking).

status="sell_pending" means a SELL order was placed but fill not yet confirmed.
Reconciler verifies actual fill quantity and handles partial exits.

Revision ID: 015
Revises: 014
Create Date: 2026-06-21
"""
from alembic import op
import sqlalchemy as sa

revision: str = "015"
down_revision: str = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("live_trades") as batch_op:
        batch_op.add_column(sa.Column("sell_order_id", sa.String, nullable=True))
        batch_op.add_column(sa.Column("sell_placed_at", sa.DateTime, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("live_trades") as batch_op:
        batch_op.drop_column("sell_placed_at")
        batch_op.drop_column("sell_order_id")
