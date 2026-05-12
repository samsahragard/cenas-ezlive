"""driver_system: bid board + approval queue + scoring + paychecks

Revision ID: 15_driver_system
Revises: 14_whatsapp_messages
Create Date: 2026-05-12

The data-model foundation for the driver bid system described in
SPEC.md (Ez Market / Ez Manage / My Profile / Pay History).

Extends two existing tables:
  - orders: adds the delivery state-machine columns (window start/end,
    customer rating, setup photo, payout snapshots, FK to assigned
    driver + approving manager, lifecycle timestamps).
  - drivers: adds the tier/score/lifetime/status fields the bid system
    relies on.

Creates five new tables:
  - delivery_request: every driver's request to take a delivery, with
    manager decision tracking. Unique on (delivery, driver) — one
    request per driver per order.
  - driver_score: nightly recompute snapshots of the rolling 30-day
    6-metric scoring. Latest row per driver drives the My Profile page.
  - paycheck: closed bi-weekly paychecks; deliveries FK back to a
    paycheck row when payroll finalizes them.
  - cancellation: log entries for driver-initiated cancellations after
    manager approval — drives the 30-day / 90-day threshold rules.
  - manager_message: outbound messages from manager → driver during
    active deliveries. Reply latency feeds the "Manager response time"
    scoring metric.

Render note: this migration is *documentation*. Alembic isn't wired on
the live service — create_all() handles new tables and the boot-time
idempotent column backfill in app/__init__.py handles new columns on
existing tables. Keeping the migration file in lockstep with the
models so future alembic-Pre-Deploy environments stay correct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "15_driver_system"
down_revision: Union[str, Sequence[str], None] = "14_whatsapp_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- extend orders with delivery state-machine columns ---
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(sa.Column("delivery_window_start", sa.DateTime, nullable=True))
        batch_op.add_column(sa.Column("delivery_window_end", sa.DateTime, nullable=True))
        batch_op.add_column(sa.Column("customer_rating", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("setup_photo_url", sa.String(500), nullable=True))
        batch_op.add_column(sa.Column("setup_photo_uploaded_at", sa.DateTime, nullable=True))
        batch_op.add_column(sa.Column("potential_payout", sa.Float, nullable=True))
        batch_op.add_column(sa.Column("paid_payout", sa.Float, nullable=True))
        batch_op.add_column(sa.Column("paycheck_id", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("assigned_driver_id", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("approved_by_user_id", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("approved_at", sa.DateTime, nullable=True))
        batch_op.add_column(sa.Column("pickup_actual_at", sa.DateTime, nullable=True))
        batch_op.add_column(sa.Column("en_route_at", sa.DateTime, nullable=True))
        batch_op.add_column(sa.Column("delivered_actual_at", sa.DateTime, nullable=True))
    op.create_index("idx_orders_assigned_driver_id", "orders", ["assigned_driver_id"])
    op.create_index("idx_orders_window_start", "orders", ["delivery_window_start"])

    # --- extend drivers with tier/score/lifetime/status fields ---
    with op.batch_alter_table("drivers") as batch_op:
        batch_op.add_column(sa.Column("status", sa.String(20), nullable=False, server_default="active"))
        batch_op.add_column(sa.Column("terminated_at", sa.DateTime, nullable=True))
        batch_op.add_column(sa.Column("termination_reason", sa.String(200), nullable=True))
        batch_op.add_column(sa.Column("joined_at", sa.Date, nullable=True))
        batch_op.add_column(sa.Column("lifetime_delivery_count", sa.Integer, nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("current_score", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("current_tier", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("home_store_id", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("last_known_lat", sa.Float, nullable=True))
        batch_op.add_column(sa.Column("last_known_lng", sa.Float, nullable=True))
        batch_op.add_column(sa.Column("last_location_at", sa.DateTime, nullable=True))
        batch_op.add_column(sa.Column("photo_url", sa.String(500), nullable=True))
    op.create_index("idx_drivers_status", "drivers", ["status"])
    op.create_index("idx_drivers_tier", "drivers", ["current_tier"])

    # --- delivery_request: bid log ---
    op.create_table(
        "delivery_request",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("delivery_id", sa.Integer, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("driver_id", sa.Integer, sa.ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_at", sa.DateTime, nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("decided_at", sa.DateTime, nullable=True),
        sa.Column("decided_by_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("delivery_id", "driver_id", name="uq_delivery_request_delivery_driver"),
    )
    op.create_index("idx_delivery_request_delivery_status", "delivery_request", ["delivery_id", "status"])
    op.create_index("idx_delivery_request_driver_status", "delivery_request", ["driver_id", "status"])

    # --- driver_score: nightly recompute snapshots ---
    op.create_table(
        "driver_score",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("driver_id", sa.Integer, sa.ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("computed_at", sa.DateTime, nullable=False),
        sa.Column("window_start", sa.Date, nullable=False),
        sa.Column("window_end", sa.Date, nullable=False),
        sa.Column("score", sa.Integer, nullable=False),
        sa.Column("tier", sa.String(20), nullable=False),
        sa.Column("tracking_pts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("on_time_pts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cancellation_pts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("photo_pts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("response_pts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("star_pts", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("idx_driver_score_driver_computed", "driver_score", ["driver_id", "computed_at"])

    # --- paycheck: closed bi-weekly paychecks ---
    op.create_table(
        "paycheck",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("driver_id", sa.Integer, sa.ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pay_period_start", sa.Date, nullable=False),
        sa.Column("pay_period_end", sa.Date, nullable=False),
        sa.Column("closed_at", sa.DateTime, nullable=False),
        sa.Column("gross_amount", sa.Float, nullable=False),
        sa.Column("net_amount", sa.Float, nullable=True),
        sa.Column("check_reference", sa.String(100), nullable=True),
    )
    op.create_index("idx_paycheck_driver_period", "paycheck", ["driver_id", "pay_period_end"])

    # --- cancellation: log of driver-initiated cancellations ---
    op.create_table(
        "cancellation",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("delivery_id", sa.Integer, sa.ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
        sa.Column("driver_id", sa.Integer, sa.ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cancelled_at", sa.DateTime, nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("cancelled_by", sa.String(20), nullable=False, server_default="driver"),
    )
    op.create_index("idx_cancellation_driver_at", "cancellation", ["driver_id", "cancelled_at"])

    # --- manager_message: manager → driver messages with reply latency ---
    op.create_table(
        "manager_message",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("sender_user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("driver_id", sa.Integer, sa.ForeignKey("drivers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("delivery_id", sa.Integer, sa.ForeignKey("orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("sent_at", sa.DateTime, nullable=False),
        sa.Column("replied_at", sa.DateTime, nullable=True),
        sa.Column("replied_within_seconds", sa.Integer, nullable=True),
        sa.Column("during_active_delivery", sa.Boolean, nullable=False, server_default="0"),
    )
    op.create_index("idx_manager_message_driver_at", "manager_message", ["driver_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("idx_manager_message_driver_at", "manager_message")
    op.drop_table("manager_message")
    op.drop_index("idx_cancellation_driver_at", "cancellation")
    op.drop_table("cancellation")
    op.drop_index("idx_paycheck_driver_period", "paycheck")
    op.drop_table("paycheck")
    op.drop_index("idx_driver_score_driver_computed", "driver_score")
    op.drop_table("driver_score")
    op.drop_index("idx_delivery_request_driver_status", "delivery_request")
    op.drop_index("idx_delivery_request_delivery_status", "delivery_request")
    op.drop_table("delivery_request")
    op.drop_index("idx_drivers_tier", "drivers")
    op.drop_index("idx_drivers_status", "drivers")
    with op.batch_alter_table("drivers") as batch_op:
        for col in (
            "photo_url", "last_location_at", "last_known_lng", "last_known_lat",
            "home_store_id", "current_tier", "current_score",
            "lifetime_delivery_count", "joined_at", "termination_reason",
            "terminated_at", "status",
        ):
            batch_op.drop_column(col)
    op.drop_index("idx_orders_window_start", "orders")
    op.drop_index("idx_orders_assigned_driver_id", "orders")
    with op.batch_alter_table("orders") as batch_op:
        for col in (
            "delivered_actual_at", "en_route_at", "pickup_actual_at",
            "approved_at", "approved_by_user_id", "assigned_driver_id",
            "paycheck_id", "paid_payout", "potential_payout",
            "setup_photo_uploaded_at", "setup_photo_url", "customer_rating",
            "delivery_window_end", "delivery_window_start",
        ):
            batch_op.drop_column(col)
