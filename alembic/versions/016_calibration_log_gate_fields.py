"""Add gate verdict/direction/threshold + sigma_spread to calibration_log.

Enables direct PASS/SKIP filtering without recomputing the gate from raw fields.
Also fixes the unit bug: sigma_used was computed from group.unit (unreliable for
US cities with group.unit='C'); new rows use unit derived from bin_label.

Revision ID: 016
Revises: 015
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision: str = "016"
down_revision: str = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("calibration_log") as batch_op:
        batch_op.add_column(sa.Column("gate_verdict", sa.String(8), nullable=True))
        batch_op.add_column(sa.Column("gate_direction", sa.String(8), nullable=True))
        batch_op.add_column(sa.Column("k_sigma_threshold", sa.Float, nullable=True))
        batch_op.add_column(sa.Column("sigma_spread", sa.Float, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("calibration_log") as batch_op:
        batch_op.drop_column("sigma_spread")
        batch_op.drop_column("k_sigma_threshold")
        batch_op.drop_column("gate_direction")
        batch_op.drop_column("gate_verdict")
