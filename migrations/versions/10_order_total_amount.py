"""add total_amount to orders for ezCater revenue tracking

Revision ID: 10_order_total_amount
Revises: 9_driver_live_tracking
Create Date: 2026-05-09

ezCater orders never go through Toast, so /reports/sales has no idea how much
revenue they bring in. The Partner API has the catererTotalDue field but our
token returns 403 on it. Workaround: every order item's raw_alias is stored
as "Item Name @ $XX.XX" by the existing PDF + API ingest, so we can parse the
unit price from each line and sum qty × unit. This column caches that total
so the sales + labor reports don't have to re-walk OrderItems on every query.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "10_order_total_amount"
down_revision: Union[str, Sequence[str], None] = "9_driver_live_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("total_amount", sa.Float, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("total_amount")
