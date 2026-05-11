"""add_paper_trading_tables

Revision ID: 004
Revises: 003
Create Date: 2025-05-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Add forecast_date to weather_snapshots and rebuild unique constraint
    #    Batch mode recreates the table — required for SQLite constraint changes.
    with op.batch_alter_table("weather_snapshots", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("forecast_date", sa.Date(), nullable=True))
        batch_op.drop_constraint("uq_weather_snapshot", type_="unique")
        batch_op.create_unique_constraint(
            "uq_weather_snapshot",
            ["city", "source", "forecast_date", "snapshot_time"],
        )

    # 2. paper_trades
    op.create_table(
        "paper_trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("decision_time", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("consensus_temp", sa.Float(), nullable=True),
        sa.Column("sum_of_prices", sa.Float(), nullable=True),
        sa.Column("estimated_edge", sa.Float(), nullable=True),
        sa.Column("basket_json", sa.Text(), nullable=False),
        sa.Column("virtual_stake_usd", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), server_default="open", nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("actual_temp", sa.Float(), nullable=True),
        sa.Column("winning_market_id", sa.String(), nullable=True),
        sa.Column("pnl_usd", sa.Float(), nullable=True),
        sa.Column("rejection_reason", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["market_groups.group_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_paper_trades_group_id", "paper_trades", ["group_id"])
    op.create_index("ix_paper_trades_status", "paper_trades", ["status"])

    # 3. checklist_evaluations
    op.create_table(
        "checklist_evaluations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("rules_ok", sa.Boolean(), nullable=False),
        sa.Column("consensus_ok", sa.Boolean(), nullable=False),
        sa.Column("sum_of_prices_ok", sa.Boolean(), nullable=False),
        sa.Column("stability_ok", sa.Boolean(), nullable=False),
        sa.Column("edge_ok", sa.Boolean(), nullable=False),
        sa.Column("consensus_temp", sa.Float(), nullable=True),
        sa.Column("sum_of_prices", sa.Float(), nullable=True),
        sa.Column("estimated_edge", sa.Float(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("triggered_paper_trade", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("rejection_reason", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["market_groups.group_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_checklist_evaluations_group_id", "checklist_evaluations", ["group_id"])
    op.create_index("ix_checklist_evaluations_evaluated_at", "checklist_evaluations", ["evaluated_at"])


def downgrade() -> None:
    op.drop_table("checklist_evaluations")
    op.drop_table("paper_trades")

    with op.batch_alter_table("weather_snapshots", recreate="always") as batch_op:
        batch_op.drop_column("forecast_date")
        batch_op.drop_constraint("uq_weather_snapshot", type_="unique")
        batch_op.create_unique_constraint(
            "uq_weather_snapshot", ["city", "source", "snapshot_time"]
        )
