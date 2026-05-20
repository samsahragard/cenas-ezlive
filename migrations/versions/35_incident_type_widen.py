"""Incident type column widen — VARCHAR(40) -> VARCHAR(200) for multi-select CSV

Revision ID: 35_incident_type_widen
Revises: 34_incident_reports_v4_fields
Create Date: 2026-05-20

ck iteration (Sam dev chat #5:08 — incident-type grid is now multi-select,
so the column stores a CSV like "injury,equipment,food-safety,customer".
The v3 VARCHAR(40) limit truncates with all 8 types selected at ~68 chars).

Single change: ALTER COLUMN manager_incident_report.incident_type TYPE
VARCHAR(200). Pure widening — no data loss; existing single-value rows
fit comfortably.

Render note: matches the convention of migrations 8-34 — alembic isn't
wired on the live Render service. Actual schema change happens via the
idempotent boot-time backfill in app/__init__.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "35_incident_type_widen"
down_revision: Union[str, Sequence[str], None] = "34_incident_reports_v4_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("manager_incident_report", "incident_type",
                    existing_type=sa.String(40),
                    type_=sa.String(200))


def downgrade() -> None:
    op.alter_column("manager_incident_report", "incident_type",
                    existing_type=sa.String(200),
                    type_=sa.String(40))
