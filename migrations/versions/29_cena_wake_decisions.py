"""cena_wake_decisions table (Haiku classifier telemetry for cena wake decisions)

Revision ID: 29_cena_wake_decisions
Revises: 28_sample_approvals
Create Date: 2026-05-17

Per Sam #2576 6-piece architecture proposal (greenlight 2026-05-17 21:34)
+ cena #2572 refinements. Phase A piece #3 (telemetry) lands first as
it has zero behavior impact and unblocks shadow-mode measurement for
the Haiku-classifier-gated cena wake (pieces #1 + #2).

One row per dev chat message the watcher considers, regardless of
whether the watcher actually fires cena. Columns capture both the
classifier's verdict (label + confidence + reason + token + latency)
AND the watcher's actual decision (did_fire + actual_rule_trigger) so
the cena-stats dashboard can compute the would-have-fired vs did-fire
delta that drives the cutover-to-enforcement call.

shadow_mode column lets us flag rows produced under shadow vs
enforcement so post-cutover comparison stays clean.

Render note: matches the convention of migrations 8-28 — alembic
isn't wired on the live Render service. Actual CREATE TABLE happens
via the idempotent boot-time table-backfill in app/__init__.py
(table-presence-gated metadata.create_all subset).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "29_cena_wake_decisions"
down_revision: Union[str, Sequence[str], None] = "28_sample_approvals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cena_wake_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        # Linkage back to the message that triggered the classifier
        # call. Nullable so backfills / synthetic rows don't error if
        # the FK target is gone (no ondelete cascade — keep telemetry).
        sa.Column("dev_chat_message_id", sa.Integer(),
                  sa.ForeignKey("developer_chat.id"),
                  nullable=True, index=True),
        sa.Column("author", sa.String(64), nullable=True, index=True),
        # First 200 chars of the message for human readability on the
        # stats dashboard. Not authoritative — full text lives in
        # developer_chat row.
        sa.Column("message_snippet", sa.Text(), nullable=True),

        # Classifier verdict
        sa.Column("classifier_label", sa.String(32), nullable=False, index=True),
        # 'wake' | 'skip' | 'uncertain' | 'error'
        sa.Column("classifier_confidence", sa.Float(), nullable=True),
        sa.Column("classifier_reason", sa.Text(), nullable=True),
        sa.Column("classifier_model", sa.String(64), nullable=True),
        sa.Column("classifier_input_tokens", sa.Integer(), nullable=True),
        sa.Column("classifier_output_tokens", sa.Integer(), nullable=True),
        sa.Column("classifier_cache_create_tokens", sa.Integer(), nullable=True),
        sa.Column("classifier_cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("classifier_latency_ms", sa.Integer(), nullable=True),

        # Watcher's actual decision (independent of classifier in shadow mode)
        sa.Column("would_fire", sa.Boolean(), nullable=False,
                  server_default=sa.text("FALSE")),
        sa.Column("did_fire", sa.Boolean(), nullable=False,
                  server_default=sa.text("FALSE")),
        sa.Column("actual_rule_trigger", sa.String(64), nullable=True),
        # 'address_mention' | 'coalesce_batch' | 'manual' | None

        # Mode flag — TRUE while classifier is observation-only; flipped
        # when we promote classifier to gate.
        sa.Column("shadow_mode", sa.Boolean(), nullable=False,
                  server_default=sa.text("TRUE")),

        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "ix_cena_wake_decisions_created_at",
        "cena_wake_decisions", ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_cena_wake_decisions_created_at",
                  table_name="cena_wake_decisions")
    op.drop_table("cena_wake_decisions")
