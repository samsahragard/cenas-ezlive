"""Recipe table — code + english_instructions fields

Revision ID: 32_recipe_table
Revises: 31_daily_log_v3_fields
Create Date: 2026-05-19

ck build-order #3 (2026-05-19): the recipes_v3 design surfaces a
canonical recipe code (e.g. 'Hot 11', 'Sauce 2') for kitchen-label
parity with the printed SOP, and renders English + Spanish prep
steps as a language toggle. The existing Recipe table (auto-created
in app/__init__.py recipes+fresh_food block, Sam #1130-#1144) holds
spanish_instructions only — add two fields:

- code:                 VARCHAR(20)  nullable, indexed.  Kitchen ID.
- english_instructions: TEXT         nullable.            EN prep text.

All additive: new columns nullable so existing rows stay valid.

Render note: matches the convention of migrations 8-31 — alembic
isn't wired on the live Render service. Actual schema change happens
via the idempotent boot-time backfill in app/__init__.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "32_recipe_table"
down_revision: Union[str, Sequence[str], None] = "31_daily_log_v3_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("recipes",
                  sa.Column("code", sa.String(20), nullable=True))
    op.create_index("ix_recipes_code", "recipes", ["code"])
    op.add_column("recipes",
                  sa.Column("english_instructions", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("recipes", "english_instructions")
    op.drop_index("ix_recipes_code", table_name="recipes")
    op.drop_column("recipes", "code")
