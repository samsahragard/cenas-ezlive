"""add produce_price_snapshot table

Revision ID: 5_produce_price_snapshot
Revises: 4_add_external_delivery_id
Create Date: 2026-05-09

Captures the weekly vendor price sheet (Alvarado / J. Luna) over time so
the produce price-history view can chart per-item price changes and flag
"biggest movers" — Sam's deterrent against vendors quietly bumping prices.

Idempotent on (snapshot_date, vendor, canonical_name, canonical_size).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5_produce_price_snapshot"
down_revision: Union[str, Sequence[str], None] = "4_add_external_delivery_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "produce_price_snapshot",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("snapshot_date", sa.String(length=10), nullable=False),
        sa.Column("vendor", sa.String(length=50), nullable=False),
        sa.Column("canonical_name", sa.String(length=200), nullable=False),
        sa.Column("canonical_size", sa.String(length=100), nullable=True),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("raw_item_name", sa.String(length=300), nullable=True),
        sa.Column("parsed_at", sa.String(length=40), nullable=True),
        sa.Column("date_range", sa.String(length=80), nullable=True),
        sa.UniqueConstraint(
            "snapshot_date", "vendor", "canonical_name", "canonical_size",
            name="uq_pps_per_day_vendor_item",
        ),
    )
    op.create_index("ix_pps_snapshot_date", "produce_price_snapshot", ["snapshot_date"])
    op.create_index("ix_pps_vendor", "produce_price_snapshot", ["vendor"])
    op.create_index("ix_pps_canonical_name", "produce_price_snapshot", ["canonical_name"])


def downgrade() -> None:
    op.drop_index("ix_pps_canonical_name", table_name="produce_price_snapshot")
    op.drop_index("ix_pps_vendor", table_name="produce_price_snapshot")
    op.drop_index("ix_pps_snapshot_date", table_name="produce_price_snapshot")
    op.drop_table("produce_price_snapshot")
