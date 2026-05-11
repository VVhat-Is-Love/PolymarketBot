"""Initial schema

Revision ID: 001
Revises:
Create Date: 2025-05-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_groups",
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("resolution_date", sa.Date(), nullable=False),
        sa.Column("weather_station", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("group_id"),
    )
    op.create_index("ix_market_groups_city", "market_groups", ["city"], unique=False)

    op.create_table(
        "markets",
        sa.Column("market_id", sa.String(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("bin_label", sa.String(), nullable=True),
        sa.Column("bin_min", sa.Float(), nullable=True),
        sa.Column("bin_max", sa.Float(), nullable=True),
        sa.Column("end_date", sa.DateTime(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["group_id"], ["market_groups.group_id"]),
        sa.PrimaryKeyConstraint("market_id"),
    )

    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_id", sa.String(), nullable=False),
        sa.Column("best_bid", sa.Float(), nullable=True),
        sa.Column("best_ask", sa.Float(), nullable=True),
        sa.Column("best_bid_size", sa.Float(), nullable=True),
        sa.Column("best_ask_size", sa.Float(), nullable=True),
        sa.Column("mid", sa.Float(), nullable=True),
        sa.Column("spread", sa.Float(), nullable=True),
        sa.Column("price_yes", sa.Float(), nullable=True),
        sa.Column("price_no", sa.Float(), nullable=True),
        sa.Column(
            "snapshot_time",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["market_id"], ["markets.market_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_id", "snapshot_time", name="uq_market_snapshot"),
    )
    op.create_index(
        "ix_snapshot_market_time", "market_snapshots", ["market_id", "snapshot_time"]
    )
    op.create_index(
        "ix_market_snapshots_snapshot_time", "market_snapshots", ["snapshot_time"]
    )

    op.create_table(
        "weather_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("weather_station", sa.String(), nullable=True),
        sa.Column("temp_current", sa.Float(), nullable=True),
        sa.Column("temp_forecast_max", sa.Float(), nullable=True),
        sa.Column("temp_forecast_min", sa.Float(), nullable=True),
        sa.Column("humidity", sa.Float(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column(
            "snapshot_time",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("city", "source", "snapshot_time", name="uq_weather_snapshot"),
    )
    op.create_index(
        "ix_weather_city_time", "weather_snapshots", ["city", "snapshot_time"]
    )
    op.create_index(
        "ix_weather_snapshots_snapshot_time", "weather_snapshots", ["snapshot_time"]
    )


def downgrade() -> None:
    op.drop_index("ix_weather_snapshots_snapshot_time", table_name="weather_snapshots")
    op.drop_index("ix_weather_city_time", table_name="weather_snapshots")
    op.drop_table("weather_snapshots")

    op.drop_index("ix_market_snapshots_snapshot_time", table_name="market_snapshots")
    op.drop_index("ix_snapshot_market_time", table_name="market_snapshots")
    op.drop_table("market_snapshots")

    op.drop_table("markets")

    op.drop_index("ix_market_groups_city", table_name="market_groups")
    op.drop_table("market_groups")
