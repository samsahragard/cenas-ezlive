"""fix driver constraints (no-op in Claude edition)

Revision ID: 2_fix_driver_constraints
Revises: 1f867c7e66e2
Create Date: 2026-04-10

In the upstream Gemini edition this migration:
  - dropped the FK from driver_logs.driver_name to drivers.name
  - replaced the global unique on drivers.name with a composite unique on
    (name, location)
  - made driver_logs.order_link nullable

In the Claude edition the initial migration was rewritten to produce that
final schema directly, so this revision is now a no-op. Kept as a stub so
existing alembic heads (e.g. on a Postgres deploy that already ran the
old migration 1) can still upgrade through this revision id.
"""
from typing import Sequence, Union


revision: str = "2_fix_driver_constraints"
down_revision: Union[str, Sequence[str], None] = "1f867c7e66e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
