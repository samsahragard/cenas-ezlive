"""orders.status: decommission legacy 'processed' artifact

Revision ID: 24_orders_processed_to_available
Revises: 23_cena_action_log
Create Date: 2026-05-17

Sam #1646 + samai #1645 — the ingest pipeline historically wrote
Order.status='processed' as a job-completion marker (meaning "ingest
ran end-to-end: parse + normalize + kitchen breakdown + dispatch
math"). It predated the driver-bid lifecycle (added migration 15) and
was orphaned in the data plane: zero readers, zero branches, just a
silent gate that made every freshly-ingested order unrequestable on
/ez-market because 'processed' is not in the lifecycle's requestable
set ({available, requested}).

This migration corrects the semantic mismatch: orders enter the bid
pool the moment ingest completes, since Cenas's driver-bid operation
is independent of ezCater's own courier assignment (per Sam #1646).
'Ingest done' is a job-state on our side and has no bearing on
whether a Cenas driver should be allowed to request it.

Code change (paired with this migration): app/services/
persistence_service.py:99 default flipped from 'processed' to
'available'. After this lands, new ingests are auto-available; this
migration retroactively flips any legacy rows still holding the old
value.

Render note: matches the convention of migrations 15-23 — alembic
isn't wired on the live Render service. The actual data flip happens
via the idempotent boot-time backfill in app/__init__.py (mirrors
the migration-15 column backfill pattern), which runs on every boot
and is a no-op once the table is clean. This file is in lockstep
with that backfill so a future alembic-Pre-Deploy environment can
replay the change deterministically.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "24_orders_processed_to_available"
down_revision: Union[str, Sequence[str], None] = "23_cena_action_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE orders SET status='available' WHERE status='processed'"
    )


def downgrade() -> None:
    # No-op: the inverse mapping isn't unique because 'processed' rows
    # could in principle have legitimately moved to 'requested' /
    # 'approved' / ... after this migration, so a blind reverse would
    # corrupt the lifecycle state. Downgrade is intentionally a
    # documentation-only stub; if you truly need to roll back, do it
    # by hand against the audit trail.
    pass
