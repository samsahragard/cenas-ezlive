"""sales_insights: SalesInsight

Revision ID: 20_sales_insights
Revises: 19_task_system
Create Date: 2026-05-14

Phase 2 / Block 1F (samai spec) — see
/partner/developer/app/block-1f-sales-insights-spec. The producer side
of the ribbon's Sales category: one table holding time-bound external
intelligence (weather / events / school calendars / traffic / outages
/ yoy comparisons / ai-synthesized) that a daily 5am-CT synthesis cron
writes and 1C's ribbon router reads.

One new table:
  - sales_insights: ephemeral operational intelligence. NOT an audit
    log — rows are DELETEd once past valid_until_at by 1E's every-5m
    cron (escalation.py leg 3), so there is no append-only constraint.
    valid_until_at is NOT NULL so nothing lingers in the ribbon
    forever. The ix_sales_insights_live (valid_until_at, store_scope)
    composite serves both the 1C "live for this store" query and 1E's
    expiry scan. dismissed_by is a JSON list[int] of user_ids, not a
    User FK (1F spec §2.1).

Render note: this migration is *documentation* — alembic isn't wired
on the live service; Base.metadata.create_all() in app/__init__.py
handles new tables on boot. Keeping the migration file in lockstep
with the models so a future alembic-Pre-Deploy environment stays
correct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20_sales_insights"
down_revision: Union[str, Sequence[str], None] = "19_task_system"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sales_insights",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("valid_until_at", sa.DateTime, nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("store_scope", sa.String(20), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("headline", sa.String(200), nullable=False),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("source_url", sa.String(500), nullable=True),
        sa.Column("dismissed_by", sa.JSON, nullable=True),
    )
    op.create_index("ix_sales_insights_created_at", "sales_insights",
                    ["created_at"])
    op.create_index("ix_sales_insights_valid_until_at", "sales_insights",
                    ["valid_until_at"])
    op.create_index("ix_sales_insights_live", "sales_insights",
                    ["valid_until_at", "store_scope"])


def downgrade() -> None:
    op.drop_index("ix_sales_insights_live", table_name="sales_insights")
    op.drop_index("ix_sales_insights_valid_until_at",
                  table_name="sales_insights")
    op.drop_index("ix_sales_insights_created_at",
                  table_name="sales_insights")
    op.drop_table("sales_insights")
