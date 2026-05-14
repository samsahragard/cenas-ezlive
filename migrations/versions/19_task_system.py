"""task_system: Task + TaskAuditLog + RibbonItemDismissal

Revision ID: 19_task_system
Revises: 18_scheduled_event
Create Date: 2026-05-14

Phase 2 / Block 1A (samai spec) — see
/partner/developer/app/block-1a-task-system-spec. The data foundation
of the Block 1 ribbon system: the Task model, its append-only audit
log, and the ribbon-dismissal table. 1A makes tasks exist, be owned,
be reassigned, and be audited; nothing renders a ribbon in 1A.

Three new tables:
  - tasks: the unit of operational work. completed_* columns are
    defined here but written by 1D's Check handler; escalated_*
    columns are written by 1E's escalation cron.
  - task_audit_log: append-only audit trail (before_delete listener
    on the model raises). task_id + actor_user_id are ondelete=RESTRICT
    so a Task with audit history is effectively undeletable.
  - ribbon_item_dismissals: per-user, per-day "not now" dismissals.
    item_id is a polymorphic ref (NOT a DB FK). 1A ships the table;
    1D writes to it.

Render note: this migration is *documentation* — alembic isn't wired
on the live service; Base.metadata.create_all() in app/__init__.py
handles new tables on boot. Keeping the migration file in lockstep
with the models so a future alembic-Pre-Deploy environment stays
correct.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "19_task_system"
down_revision: Union[str, Sequence[str], None] = "18_scheduled_event"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- tasks ---
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("owner_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("assigned_by_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("store_scope", sa.String(20), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("deadline_at", sa.DateTime, nullable=False),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("completed_by_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("escalated_to_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("escalated_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_tasks_owner_user_id", "tasks", ["owner_user_id"])
    op.create_index("ix_tasks_deadline_at", "tasks", ["deadline_at"])
    op.create_index("ix_tasks_completed_at", "tasks", ["completed_at"])
    op.create_index("ix_tasks_escalated_to_user_id", "tasks",
                    ["escalated_to_user_id"])
    op.create_index("ix_tasks_owner_open", "tasks",
                    ["owner_user_id", "completed_at"])
    op.create_index("ix_tasks_escalation_scan", "tasks",
                    ["completed_at", "escalated_at", "deadline_at"])

    # --- task_audit_log ---
    op.create_table(
        "task_audit_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer,
                  sa.ForeignKey("tasks.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("actor_user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("details", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_task_audit_log_task_id", "task_audit_log",
                    ["task_id"])
    op.create_index("ix_task_audit_log_created_at", "task_audit_log",
                    ["created_at"])

    # --- ribbon_item_dismissals ---
    op.create_table(
        "ribbon_item_dismissals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer,
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("item_type", sa.String(20), nullable=False),
        sa.Column("item_id", sa.Integer, nullable=False),
        sa.Column("dismiss_day", sa.String(10), nullable=False),
        sa.Column("dismissed_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("user_id", "item_type", "item_id", "dismiss_day",
                            name="uq_ribbon_dismissal_per_day"),
    )
    op.create_index("ix_ribbon_dismissal_lookup", "ribbon_item_dismissals",
                    ["user_id", "dismiss_day"])


def downgrade() -> None:
    op.drop_index("ix_ribbon_dismissal_lookup",
                  table_name="ribbon_item_dismissals")
    op.drop_table("ribbon_item_dismissals")
    op.drop_index("ix_task_audit_log_created_at", table_name="task_audit_log")
    op.drop_index("ix_task_audit_log_task_id", table_name="task_audit_log")
    op.drop_table("task_audit_log")
    op.drop_index("ix_tasks_escalation_scan", table_name="tasks")
    op.drop_index("ix_tasks_owner_open", table_name="tasks")
    op.drop_index("ix_tasks_escalated_to_user_id", table_name="tasks")
    op.drop_index("ix_tasks_completed_at", table_name="tasks")
    op.drop_index("ix_tasks_deadline_at", table_name="tasks")
    op.drop_index("ix_tasks_owner_user_id", table_name="tasks")
    op.drop_table("tasks")
