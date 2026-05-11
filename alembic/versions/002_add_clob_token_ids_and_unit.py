"""add_clob_token_ids_and_unit

Revision ID: 002
Revises: 001
Create Date: 2025-05-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # market_groups: add unit and event_volume
    op.add_column("market_groups", sa.Column("unit", sa.String(1), nullable=True))
    op.add_column("market_groups", sa.Column("event_volume", sa.Float(), nullable=True))

    # markets: add condition_id, token_id_yes, token_id_no
    op.add_column("markets", sa.Column("condition_id", sa.String(), nullable=True))
    op.add_column("markets", sa.Column("token_id_yes", sa.String(), nullable=True))
    op.add_column("markets", sa.Column("token_id_no", sa.String(), nullable=True))

    # Unique index on condition_id (enforces uniqueness without ALTER TABLE CONSTRAINT)
    op.create_index("ix_markets_condition_id", "markets", ["condition_id"], unique=True)
    op.create_index("ix_markets_token_id_yes", "markets", ["token_id_yes"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_markets_token_id_yes", table_name="markets")
    op.drop_index("ix_markets_condition_id", table_name="markets")
    op.drop_column("markets", "token_id_no")
    op.drop_column("markets", "token_id_yes")
    op.drop_column("markets", "condition_id")
    op.drop_column("market_groups", "event_volume")
    op.drop_column("market_groups", "unit")
