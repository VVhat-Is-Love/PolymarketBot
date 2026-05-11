"""add_snapshot_source

Revision ID: 005
Revises: 004
Create Date: 2026-05-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("market_snapshots", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("market_snapshots", recreate="auto") as batch_op:
        batch_op.drop_column("source")
