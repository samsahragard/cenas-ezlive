"""ambient_signals: AmbientSignal + AmbientSignalRun

Revision ID: 22_ambient_signals
Revises: 21_sam_chat
Create Date: 2026-05-14

Phase 2 / Block 1J (samai spec) — see
/partner/developer/app/block-1j-ambient-signal-spec. The in-app
data-plane / control-plane separation: six per-source /cron/refresh-*
crons WRITE AmbientSignal rows; the 1C ribbon router + the
/cron/sales-insights pipeline READ them.

Two new tables:
  - ambient_signals: the data-plane row. uq_ambient_signal_identity
    (source, signal_key) is the id-stable upsert's lookup key — one
    row per logical signal, updated IN PLACE across payload refreshes
    so a RibbonItemDismissal survives a refresh (spec §2.2 / §6). NOT
    an audit log — expired rows are DELETEd by each cron's per-source
    sweep, so no append-only constraint.
  - ambient_signal_runs: one row per cron run (the operational audit).

Render note: this migration is *documentation* — alembic isn't wired
on the live service; Base.metadata.create_all() in app/__init__.py
handles new tables on boot. Kept in lockstep with the models.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "22_ambient_signals"
down_revision: Union[str, Sequence[str], None] = "21_sam_chat"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ambient_signals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("signal_key", sa.String(200), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("store_scope", sa.String(20), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("valid_until_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("last_seen_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("source", "signal_key",
                            name="uq_ambient_signal_identity"),
    )
    op.create_index("ix_ambient_signals_valid_until_at",
                    "ambient_signals", ["valid_until_at"])
    op.create_index("ix_ambient_signals_live", "ambient_signals",
                    ["valid_until_at", "store_scope"])
    op.create_index("ix_ambient_signals_source_expiry", "ambient_signals",
                    ["source", "valid_until_at"])

    op.create_table(
        "ambient_signal_runs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("signals_created", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("signals_updated", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("signals_unchanged", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("signals_expired", sa.Integer, nullable=False,
                  server_default="0"),
        sa.Column("error_text", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_ambient_signal_runs_source",
                    "ambient_signal_runs", ["source"])
    op.create_index("ix_ambient_signal_runs_created_at",
                    "ambient_signal_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ambient_signal_runs_created_at",
                  table_name="ambient_signal_runs")
    op.drop_index("ix_ambient_signal_runs_source",
                  table_name="ambient_signal_runs")
    op.drop_table("ambient_signal_runs")
    op.drop_index("ix_ambient_signals_source_expiry",
                  table_name="ambient_signals")
    op.drop_index("ix_ambient_signals_live", table_name="ambient_signals")
    op.drop_index("ix_ambient_signals_valid_until_at",
                  table_name="ambient_signals")
    op.drop_table("ambient_signals")
