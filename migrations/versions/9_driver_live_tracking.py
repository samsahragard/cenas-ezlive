"""add driver_shift + driver_location tables for live GPS tracking

Revision ID: 9_driver_live_tracking
Revises: 8_drivers_auth
Create Date: 2026-05-09

Phase B of driver tracking. A `driver_shift` is an explicit on/off-clock period
opened by the driver tapping "Start shift" in the driver portal — GPS streaming
runs only while a shift is open (privacy + battery). `driver_location` stores
each position fix from the browser's geolocation API, FK'd to its shift so the
manager can replay the route after the fact (Phase C).

Both tables are new (no ALTERs on existing tables), so create_all() will pick
them up on Render even without alembic — no startup-time backfill needed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9_driver_live_tracking"
down_revision: Union[str, Sequence[str], None] = "8_drivers_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "driver_shift",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "driver_id",
            sa.Integer,
            sa.ForeignKey("drivers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_driver_shift_driver_id", "driver_shift", ["driver_id"])
    op.create_index("ix_driver_shift_open", "driver_shift", ["driver_id", "ended_at"])

    op.create_table(
        "driver_location",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "shift_id",
            sa.Integer,
            sa.ForeignKey("driver_shift.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "driver_id",
            sa.Integer,
            sa.ForeignKey("drivers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lng", sa.Float, nullable=False),
        sa.Column("accuracy_m", sa.Float, nullable=True),
        sa.Column("speed_mps", sa.Float, nullable=True),
        sa.Column("heading_deg", sa.Float, nullable=True),
    )
    op.create_index("ix_driver_location_shift", "driver_location", ["shift_id"])
    op.create_index("ix_driver_location_driver_captured", "driver_location",
                    ["driver_id", "captured_at"])


def downgrade() -> None:
    op.drop_index("ix_driver_location_driver_captured", table_name="driver_location")
    op.drop_index("ix_driver_location_shift", table_name="driver_location")
    op.drop_table("driver_location")
    op.drop_index("ix_driver_shift_open", table_name="driver_shift")
    op.drop_index("ix_driver_shift_driver_id", table_name="driver_shift")
    op.drop_table("driver_shift")
