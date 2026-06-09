"""ezCater route-history samples

Revision ID: 42_ezcater_route_history
Revises: 41_driver_payroll_inputs
Create Date: 2026-06-09

Live ezCater tracking exposes the driver's current location, not a finished
route history. Capture sampled points while tracking is live so managers and
drivers can review the route, route time, and route miles after ezCater's
customer-facing tracker expires.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "42_ezcater_route_history"
down_revision: Union[str, Sequence[str], None] = "41_driver_payroll_inputs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ezcater_tracking_point",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("driver_id", sa.Integer(), nullable=True),
        sa.Column("tracking_uuid", sa.String(length=64), nullable=False),
        sa.Column("driver_name", sa.String(length=150), nullable=True),
        sa.Column("provider_status_key", sa.String(length=60), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["driver_id"], ["drivers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ezcater_tracking_point_order_id", "ezcater_tracking_point", ["order_id"])
    op.create_index("ix_ezcater_tracking_point_driver_id", "ezcater_tracking_point", ["driver_id"])
    op.create_index("ix_ezcater_tracking_point_tracking_uuid", "ezcater_tracking_point", ["tracking_uuid"])
    op.create_index("ix_ezcater_tracking_point_captured_at", "ezcater_tracking_point", ["captured_at"])
    op.create_index("ix_ezcater_tracking_point_order_time", "ezcater_tracking_point", ["order_id", "captured_at"])
    op.create_index("ix_ezcater_tracking_point_driver_time", "ezcater_tracking_point", ["driver_id", "captured_at"])
    op.create_index("ix_ezcater_tracking_point_uuid_time", "ezcater_tracking_point", ["tracking_uuid", "captured_at"])


def downgrade() -> None:
    op.drop_index("ix_ezcater_tracking_point_uuid_time", table_name="ezcater_tracking_point")
    op.drop_index("ix_ezcater_tracking_point_driver_time", table_name="ezcater_tracking_point")
    op.drop_index("ix_ezcater_tracking_point_order_time", table_name="ezcater_tracking_point")
    op.drop_index("ix_ezcater_tracking_point_captured_at", table_name="ezcater_tracking_point")
    op.drop_index("ix_ezcater_tracking_point_tracking_uuid", table_name="ezcater_tracking_point")
    op.drop_index("ix_ezcater_tracking_point_driver_id", table_name="ezcater_tracking_point")
    op.drop_index("ix_ezcater_tracking_point_order_id", table_name="ezcater_tracking_point")
    op.drop_table("ezcater_tracking_point")
