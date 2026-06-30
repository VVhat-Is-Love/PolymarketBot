"""Add resolution fields to calibration_log table.

Adds bin_distance_sigma, bot_traded, resolved_outcome, resolved_at,
actual_temp_resolve — enables reliability-curve / calibration analysis
by linking each snapshot row to the actual market outcome.

Revision ID: 013
Revises: 012
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa

revision: str = "013"
down_revision: str = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("calibration_log") as batch_op:
        batch_op.add_column(sa.Column("bin_distance_sigma", sa.Float, nullable=True))
        batch_op.add_column(sa.Column(
            "bot_traded", sa.Boolean, nullable=False, server_default="0"
        ))
        batch_op.add_column(sa.Column("resolved_outcome", sa.Boolean, nullable=True))
        batch_op.add_column(sa.Column("resolved_at", sa.DateTime, nullable=True))
        batch_op.add_column(sa.Column("actual_temp_resolve", sa.Float, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("calibration_log") as batch_op:
        batch_op.drop_column("actual_temp_resolve")
        batch_op.drop_column("resolved_at")
        batch_op.drop_column("resolved_outcome")
        batch_op.drop_column("bot_traded")
        batch_op.drop_column("bin_distance_sigma")
