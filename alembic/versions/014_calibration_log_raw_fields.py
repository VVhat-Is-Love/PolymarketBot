"""Add raw σ-independent fields to calibration_log.

Adds consensus_temp, bin_near_boundary, bin_far_boundary, sigma_used.
These are σ-independent: when INTERIM-ENTRY changes σ (1.7→3-4°F),
existing bin_distance_sigma rows become incomparable (denominator shifts).
Raw fields let the strategist recompute distance at any σ post-hoc:

    realized_error   = actual_temp_resolve - consensus_temp
    empirical_sigma  = stdev(realized_error) per city / season
    distance_at_σ_x  = abs(bin_near_boundary - consensus_temp) / sigma_x

Revision ID: 014
Revises: 013
Create Date: 2026-06-19
"""
from alembic import op
import sqlalchemy as sa

revision: str = "014"
down_revision: str = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("calibration_log") as batch_op:
        batch_op.add_column(sa.Column("consensus_temp", sa.Float, nullable=True))
        batch_op.add_column(sa.Column("bin_near_boundary", sa.Float, nullable=True))
        batch_op.add_column(sa.Column("bin_far_boundary", sa.Float, nullable=True))
        batch_op.add_column(sa.Column("sigma_used", sa.Float, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("calibration_log") as batch_op:
        batch_op.drop_column("sigma_used")
        batch_op.drop_column("bin_far_boundary")
        batch_op.drop_column("bin_near_boundary")
        batch_op.drop_column("consensus_temp")
