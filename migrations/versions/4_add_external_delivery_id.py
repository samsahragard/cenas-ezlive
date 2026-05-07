"""add external_delivery_id (ezCater Delivery UUID) to Order

Revision ID: 4_add_external_delivery_id
Revises: 3_persist_dispatch
Create Date: 2026-05-07

Stores the ezCater delivery UUID per order so the "unassign auto-assigned
courier" button in Cenas EZLive can call the courierUnassign API mutation
without having to do a roundtrip API lookup by orderNumber.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4_add_external_delivery_id"
down_revision: Union[str, Sequence[str], None] = "3_persist_dispatch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("external_delivery_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("external_delivery_id")
