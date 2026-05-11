"""live_trades table

Revision ID: 007
Revises: 006
Create Date: 2026-05-09

"""
from alembic import op
import sqlalchemy as sa

revision: str = "007"
down_revision: str = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_trades",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("group_id", sa.String, sa.ForeignKey("market_groups.group_id"), nullable=True),
        sa.Column("market_id", sa.String, nullable=False),
        sa.Column("bin_label", sa.String, nullable=False),
        sa.Column("side", sa.String(4), server_default="buy", nullable=False),
        sa.Column("order_id", sa.String, nullable=True),
        sa.Column("target_price", sa.Float, nullable=True),
        sa.Column("filled_price", sa.Float, nullable=True),
        sa.Column("size_shares", sa.Float, nullable=True),
        sa.Column("stake_usd", sa.Float, nullable=True),
        sa.Column("status", sa.String, server_default="pending", nullable=False),
        sa.Column("basket_role", sa.String, nullable=True),
        sa.Column("placed_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("filled_at", sa.DateTime, nullable=True),
        sa.Column("cancelled_at", sa.DateTime, nullable=True),
        sa.Column("pnl_usd", sa.Float, nullable=True),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
        sa.Column("actual_temp", sa.Float, nullable=True),
        sa.Column("city", sa.String, nullable=True),
    )
    op.create_index("ix_live_trades_group", "live_trades", ["group_id"])
    op.create_index("ix_live_trades_status", "live_trades", ["status"])
    op.create_index("ix_live_trades_placed_at", "live_trades", ["placed_at"])
    op.create_index("ix_live_trades_order_id", "live_trades", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_live_trades_order_id", "live_trades")
    op.drop_index("ix_live_trades_placed_at", "live_trades")
    op.drop_index("ix_live_trades_status", "live_trades")
    op.drop_index("ix_live_trades_group", "live_trades")
    op.drop_table("live_trades")
