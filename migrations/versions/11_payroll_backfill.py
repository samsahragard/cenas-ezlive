"""payroll backfill: order tracking + financial fields, ezcater_known_driver

Revision ID: 11_payroll_backfill
Revises: 10_order_total_amount
Create Date: 2026-05-10

Adds the fields needed to back the per-driver payroll page Sam requested:

  Orders table — per-order ezCater report data:
    tracking_status         Tracked / Partially tracked / Untracked
    ezcater_driver_name     full name as it appears in ezCater (CK #1 - Name)
    pickup_kitchen          'copperfield' or 'tomball' (physical prep kitchen)
    pickup_miles            one-way miles, kitchen -> drop-off (Google Routes)
    food_total              ezCater's authoritative food subtotal
    tip_amount              tip $ paid by customer
    delivery_fee            delivery fee $ collected
    caterer_total_due       what ezCater pays the caterer for this order
    delivery_result         'On time' / 'late' bucket from Performance Report
    delivery_start_time     time stamp from Performance Report
    delivery_complete_time  time stamp from Performance Report

  New table ezcater_known_driver — manager-maintained roster of ezCater
  drivers we recognize. A signed-up Driver becomes 'verified' when their
  signup phone matches a row here.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "11_payroll_backfill"
down_revision: Union[str, Sequence[str], None] = "10_order_total_amount"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_ORDER_COLS = [
    ("tracking_status",         sa.String(40)),
    ("ezcater_driver_name",     sa.String(150)),
    ("pickup_kitchen",          sa.String(20)),
    ("pickup_miles",            sa.Float),
    ("food_total",              sa.Float),
    ("tip_amount",              sa.Float),
    ("delivery_fee",            sa.Float),
    ("caterer_total_due",       sa.Float),
    ("delivery_result",         sa.String(60)),
    ("delivery_start_time",     sa.String(20)),
    ("delivery_complete_time",  sa.String(20)),
]


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch:
        for name, coltype in _NEW_ORDER_COLS:
            batch.add_column(sa.Column(name, coltype, nullable=True))

    op.create_table(
        "ezcater_known_driver",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("phone_e164", sa.String(20), nullable=False),
        # 1 = Copperfield kitchen (UNO MAS), 2 = Tomball kitchen (DOS MAS),
        # NULL = ambiguous (e.g. Angelica Truss / James Paddie — no CK# prefix).
        sa.Column("ck_prefix", sa.Integer, nullable=True),
        sa.UniqueConstraint("phone_e164", name="uq_ezcater_known_driver_phone"),
    )
    op.create_index("ix_ezcater_known_driver_phone", "ezcater_known_driver", ["phone_e164"])


def downgrade() -> None:
    op.drop_index("ix_ezcater_known_driver_phone", table_name="ezcater_known_driver")
    op.drop_table("ezcater_known_driver")
    with op.batch_alter_table("orders") as batch:
        for name, _ in _NEW_ORDER_COLS:
            batch.drop_column(name)
