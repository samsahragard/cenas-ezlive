"""ezCater live tracking columns on orders.

Revision ID: 12_ezcater_live_tracking
Revises: 11_payroll_backfill
Create Date: 2026-05-11

Sam (2026-05-11): ezCater exposes a public live-tracking API at
https://delivery-management.ezcater.com/delivery_tracking/v1/delivery/<uuid>
that returns the driver's current GPS + status key per delivery. The
catch is the UUID in that URL (the "delivery_tracking_id") is distinct
from the deliveryId in our partner-API webhook. So we capture it
manually per order for now and store latest poll results inline on
the Order row.

Five new columns:
  delivery_tracking_id          UUID string
  ezcater_status_key            'driver_en_route_to_pickup' etc.
  ezcater_driver_lat            float
  ezcater_driver_lng            float
  ezcater_status_updated_at     datetime — when we last polled
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "12_ezcater_live_tracking"
down_revision: Union[str, Sequence[str], None] = "11_payroll_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_COLS = [
    ("delivery_tracking_id",      sa.String(64)),
    ("ezcater_status_key",        sa.String(60)),
    ("ezcater_driver_lat",        sa.Float),
    ("ezcater_driver_lng",        sa.Float),
    ("ezcater_status_updated_at", sa.DateTime),
]


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        for name, coltype in _NEW_COLS:
            batch.add_column(sa.Column(name, coltype, nullable=True))
    op.create_index("ix_orders_delivery_tracking_id", "orders", ["delivery_tracking_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_delivery_tracking_id", table_name="orders")
    with op.batch_alter_table("orders") as batch:
        for name, _ in _NEW_COLS:
            batch.drop_column(name)
