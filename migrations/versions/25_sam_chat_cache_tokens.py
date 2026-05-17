"""sam_chat_messages: add cache-token accounting columns

Revision ID: 25_sam_chat_cache_tokens
Revises: 24_orders_processed_to_available
Create Date: 2026-05-17

Sam #1895 prompt-caching SECONDARY per samai #2058 amendment. The
gateway-side primary (cena_gateway.py:759, aick #1921) now emits
cache_creation_input_tokens + cache_read_input_tokens on every SSE
done event. sam_chat.py's consumer captures them and persists into
the new columns so the cache-savings accounting Sam's #1895 cost-
impact projection promised becomes queryable.

Both columns are nullable; rows persisted before this migration just
carry NULL. _estimate_cost reads them with safe defaults of 0.

Render note: matches the convention of migrations 8-24 — alembic
isn't wired on the live Render service. The actual ALTER TABLE
happens via the idempotent boot-time column-backfill in
app/__init__.py (mirrors the migration-15 pattern), which runs on
every boot and is a no-op once the columns exist. This file is in
lockstep with that backfill so a future alembic-Pre-Deploy
environment can replay the change deterministically.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "25_sam_chat_cache_tokens"
down_revision: Union[str, Sequence[str], None] = "24_orders_processed_to_available"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sam_chat_messages",
                  sa.Column("cost_cache_creation_tokens",
                            sa.Integer(), nullable=True))
    op.add_column("sam_chat_messages",
                  sa.Column("cost_cache_read_tokens",
                            sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sam_chat_messages", "cost_cache_read_tokens")
    op.drop_column("sam_chat_messages", "cost_cache_creation_tokens")
