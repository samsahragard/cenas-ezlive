"""drop whatsapp_messages: Track 3 destructive teardown

Revision ID: 27_drop_whatsapp_messages
Revises: 26_dev_chat_attribution_corrections
Create Date: 2026-05-17

Per cena #2257 (Sam green-light on all 4 remaining Track 3 items),
the WhatsApp ingest pipeline is fully retired. The route + template
+ ck_whatsapp.py + WhatsAppMessage model + WhatsApp tables in
awareness.db + ck-side runtime were already removed in 72c46a5
(samai PASS #2117). The CK secrets token + AiCk-side OpenClaw
connection were removed by ck (ck #2265). This migration drops the
legacy whatsapp_messages table on Render Postgres so the schema
matches the live code with no orphan tables.

Render note: matches the convention of migrations 8-26 — alembic
isn't wired on the live Render service. The actual DROP TABLE
happens via an idempotent boot-time migration in app/__init__.py
(table-presence-gated; no-op once dropped).

Reversibility: this is destructive (data loss on the
whatsapp_messages table contents). Authorized per cena #2257 chain
back to Sam direct via cena #2243.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "27_drop_whatsapp_messages"
down_revision: Union[str, Sequence[str], None] = "26_dev_chat_attribution_corrections"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS whatsapp_messages")


def downgrade() -> None:
    # No-op: the WhatsApp pipeline is fully gone (route, model,
    # template, ck-side runtime all removed in 72c46a5). Recreating
    # the table here would yield an orphan structure with no live
    # writers. If a future re-introduction is needed, the schema
    # should come from a fresh forward migration not this revert.
    pass
