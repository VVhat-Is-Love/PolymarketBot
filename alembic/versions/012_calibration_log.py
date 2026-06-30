"""Add calibration_log table for model-vs-market calibration tracking.

One row per active bin per snapshot cycle. Pure logging — never read by
trading logic. Enables offline analysis of whether the model is
systematically better than market pricing.

Revision ID: 012
Revises: 011
Create Date: 2026-06-12
"""
from alembic import op
import sqlalchemy as sa

revision: str = "012"
down_revision: str = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calibration_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime, nullable=False, index=True),
        sa.Column("city", sa.String(64), nullable=False),
        sa.Column("forecast_date", sa.Date, nullable=False),
        sa.Column("bin_label", sa.String(64), nullable=False),
        sa.Column("model_p_no", sa.Float, nullable=True),
        sa.Column("market_no_ask", sa.Float, nullable=True),
        sa.Column("market_yes_price", sa.Float, nullable=True),
        sa.Column("volume_usd", sa.Float, nullable=True),
        sa.Column("models_count", sa.Integer, nullable=True),
    )
    op.create_index("ix_cal_city_date", "calibration_log", ["city", "forecast_date"])


def downgrade() -> None:
    op.drop_table("calibration_log")
