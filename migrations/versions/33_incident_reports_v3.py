"""Incident Reports v3 fields — severity / status / incident_type / archived_at / report_id

Revision ID: 33_incident_reports_v3
Revises: 32_recipe_table
Create Date: 2026-05-19

ck build-order (Sam dev chat #10:11 — convert Incident Reports v1 text-
heavy shell to the v3 design samai built at #6:27 + dck phone view at
#6:39). The v3 design adds structured status + severity + incident-type
fields, a human-readable report ID, and an archived-at column for the
'lock + move to archive' lifecycle.

Additive columns on the existing manager_incident_report table:

- severity:     VARCHAR(20)   NOT NULL default 'moderate'    (critical / serious / moderate / minor)
- status:       VARCHAR(20)   NOT NULL default 'open'        (open / review / locked / resolved)
- incident_type:VARCHAR(40)   NULL                           (guest_injury / employee_injury / termination / theft / food_safety / complaint / etc)
- report_id:    VARCHAR(40)   NULL  INDEX                    (IR-YYYY-MMDD-NNN human-readable)
- archived_at:  DATETIME      NULL                           (NULL = active, set = moved to archive view)

All additive: existing rows stay valid (defaults apply).

Render note: matches the convention of migrations 8-32 — alembic isn't
wired on the live Render service. Actual schema change happens via the
idempotent boot-time backfill in app/__init__.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "33_incident_reports_v3"
down_revision: Union[str, Sequence[str], None] = "32_recipe_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("manager_incident_report",
                  sa.Column("severity", sa.String(20), nullable=False,
                            server_default="moderate"))
    op.add_column("manager_incident_report",
                  sa.Column("status", sa.String(20), nullable=False,
                            server_default="open"))
    op.add_column("manager_incident_report",
                  sa.Column("incident_type", sa.String(40), nullable=True))
    op.add_column("manager_incident_report",
                  sa.Column("report_id", sa.String(40), nullable=True))
    op.create_index("ix_manager_incident_report_report_id",
                    "manager_incident_report", ["report_id"])
    op.add_column("manager_incident_report",
                  sa.Column("archived_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("manager_incident_report", "archived_at")
    op.drop_index("ix_manager_incident_report_report_id",
                  table_name="manager_incident_report")
    op.drop_column("manager_incident_report", "report_id")
    op.drop_column("manager_incident_report", "incident_type")
    op.drop_column("manager_incident_report", "status")
    op.drop_column("manager_incident_report", "severity")
