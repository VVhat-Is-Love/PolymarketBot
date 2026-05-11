"""batch_fixes_v2

Revision ID: 006
Revises: 005
Create Date: 2026-05-08

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── paper_trades: new tracking columns ────────────────────────────
    with op.batch_alter_table("paper_trades", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("resolved_via", sa.String(32), nullable=True))
        batch_op.add_column(
            sa.Column("basket_miss", sa.Boolean(), nullable=True, server_default="0")
        )
        batch_op.add_column(sa.Column("bin_volumes_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("consensus_offset", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("city", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("market_type", sa.String(), nullable=True))

    # ── rejected_markets: log cold-start / low-volume rejections ──────
    op.create_table(
        "rejected_markets",
        sa.Column("id", sa.Integer(), nullable=False, autoincrement=True),
        sa.Column("market_id", sa.String(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rejected_group", "rejected_markets", ["group_id"])
    op.create_index("ix_rejected_at", "rejected_markets", ["rejected_at"])

    # ── daily_summaries: nightly PnL snapshots ────────────────────────
    op.create_table(
        "daily_summaries",
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("total_pnl", sa.Float(), nullable=True),
        sa.Column("trades_count", sa.Integer(), nullable=True),
        sa.Column("wins", sa.Integer(), nullable=True),
        sa.Column("losses", sa.Integer(), nullable=True),
        sa.Column("basket_misses", sa.Integer(), nullable=True),
        sa.Column("open_positions", sa.Integer(), nullable=True),
        sa.Column("hwm", sa.Float(), nullable=True),
        sa.Column("summary_json", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("date"),
    )


def downgrade() -> None:
    op.drop_table("daily_summaries")
    op.drop_index("ix_rejected_at", "rejected_markets")
    op.drop_index("ix_rejected_group", "rejected_markets")
    op.drop_table("rejected_markets")
    with op.batch_alter_table("paper_trades", recreate="auto") as batch_op:
        batch_op.drop_column("market_type")
        batch_op.drop_column("city")
        batch_op.drop_column("consensus_offset")
        batch_op.drop_column("bin_volumes_json")
        batch_op.drop_column("basket_miss")
        batch_op.drop_column("resolved_via")
