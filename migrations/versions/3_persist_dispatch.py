"""persist dispatch fields on Order

Revision ID: 3_persist_dispatch
Revises: 2_fix_driver_constraints
Create Date: 2026-05-05

Adds 5 columns to `orders` so per-order views can render the Driver /
Prep Expo / Master tabs without re-running the Google Maps + dispatch
stack: kitchen_ready_time, driver_departure_time, assigned_driver,
route_group_id, route_stop_index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3_persist_dispatch"
down_revision: Union[str, Sequence[str], None] = "2_fix_driver_constraints"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.add_column(sa.Column("kitchen_ready_time", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("driver_departure_time", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("assigned_driver", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("route_group_id", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("route_stop_index", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        batch.drop_column("route_stop_index")
        batch.drop_column("route_group_id")
        batch.drop_column("assigned_driver")
        batch.drop_column("driver_departure_time")
        batch.drop_column("kitchen_ready_time")
