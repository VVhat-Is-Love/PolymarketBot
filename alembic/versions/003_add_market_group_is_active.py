"""add_market_group_is_active

Revision ID: 003
Revises: 002
Create Date: 2025-05-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "market_groups",
        sa.Column("is_active", sa.Boolean(), nullable=True),
    )
    # Mark all existing groups as active so they're not silently dropped
    op.execute("UPDATE market_groups SET is_active = 1")


def downgrade() -> None:
    op.drop_column("market_groups", "is_active")
