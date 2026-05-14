"""Tasks blueprint — create + reassign operational tasks.

Phase 2 / Block 1A (samai spec). The two write routes that make Tasks
exist and be reassigned:

  POST /partner/tasks/create          — create a task
  POST /partner/tasks/<id>/reassign   — reassign a task to a new owner

Authorization model (spec §6):
  - NO new requires_permission tag, NO partner gate. Task
    create/own/reassign is available to every authenticated keypad
    user — the directive's "anyone can assign to themselves" means
    hourly-tier users (a driver self-assigning a task) must reach
    these routes. The blueprint registers /partner/tasks/* directly
    (like developer_chat), so it is not auto-gated by store_bp's
    /partner/* before_request.
  - can_assign_to(actor, target) IS the authorization gate for the
    assignment relationship. It is enforced BEFORE any row is written
    (samai methodology rule 1 — audience-eligibility-before-mutation):
    a 403 leaves no Task row and no audit row behind.
  - The route still requires an authenticated keypad user
    (g.current_user) — _require_user() rejects partner-password-only
    or anonymous sessions with 403.

1A ships ONLY these two routes. The X/Check controls + dismiss/check
routes are 1D; the escalation cron is 1E; task completion is 1D's
Check handler. 1A's routes only ever emit the "created" and
"reassigned" TaskAuditLog actions (spec §7).
"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, abort, g, jsonify, request

from app.db import SessionLocal
from app.models import (
    Task,
    TaskAuditLog,
    User,
    _VALID_STORE_SCOPES,
    _VALID_CATEGORIES,
)
from app.services.role_hierarchy import can_assign_to


tasks_bp = Blueprint("tasks", __name__)


def _require_user() -> User:
    """Return the authenticated keypad user, or abort 403.

    Tasks need a real User (g.current_user) — they are owned by people,
    and can_assign_to compares two User rows. A partner-password-only
    or anonymous session has no g.current_user and cannot create or
    reassign tasks."""
    u = getattr(g, "current_user", None)
    if u is None:
        abort(403, description="task routes require an authenticated user")
    return u


def _parse_deadline(raw: str | None) -> datetime:
    """Parse the deadline_at form value to a datetime, or abort 400.
    Must parse as ISO 8601 and be present-or-future (spec §6.1)."""
    if not raw or not raw.strip():
        abort(400, description="deadline_at is required")
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        abort(400, description="deadline_at must be ISO 8601")
    if dt < datetime.utcnow():
        abort(400, description="deadline_at must be present or future")
    return dt


def _load_active_user(db, user_id: int) -> User:
    """Load a User by id; abort 400 if missing or inactive."""
    u = db.get(User, user_id)
    if u is None or not u.active:
        abort(400, description=f"user {user_id} not found or inactive")
    return u


def _task_json(t: Task) -> dict:
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "owner_user_id": t.owner_user_id,
        "assigned_by_user_id": t.assigned_by_user_id,
        "store_scope": t.store_scope,
        "category": t.category,
        "deadline_at": t.deadline_at.isoformat() if t.deadline_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@tasks_bp.route("/partner/tasks/create", methods=["POST"])
def create_task():
    """Create a task. Spec §6.1 — validate, enforce can_assign_to
    BEFORE any write, INSERT Task + a 'created' TaskAuditLog row."""
    actor = _require_user()

    # 1. Resolve owner — default to self-assignment.
    raw_owner = (request.form.get("owner_user_id") or "").strip()
    if raw_owner:
        try:
            owner_id = int(raw_owner)
        except ValueError:
            abort(400, description="owner_user_id must be an integer")
    else:
        owner_id = actor.id

    # 2. Validate the rest of the form.
    title = (request.form.get("title") or "").strip()
    if not title:
        abort(400, description="title is required")
    if len(title) > 200:
        abort(400, description="title must be <= 200 chars")
    description = (request.form.get("description") or "").strip() or None
    store_scope = (request.form.get("store_scope") or "").strip()
    if store_scope not in _VALID_STORE_SCOPES:
        abort(400, description=f"store_scope must be one of {sorted(_VALID_STORE_SCOPES)}")
    category = (request.form.get("category") or "").strip()
    if category not in _VALID_CATEGORIES:
        abort(400, description=f"category must be one of {sorted(_VALID_CATEGORIES)}")
    deadline_at = _parse_deadline(request.form.get("deadline_at"))

    db = SessionLocal()
    try:
        # 3. Load the owner User.
        owner = _load_active_user(db, owner_id)

        # 4. Enforce can_assign_to BEFORE any write (rule 1).
        if not can_assign_to(actor, owner):
            abort(403, description="you may not assign a task to that user")

        # 5. INSERT the Task.
        task = Task(
            title=title,
            description=description,
            owner_user_id=owner.id,
            assigned_by_user_id=actor.id,
            store_scope=store_scope,
            category=category,
            deadline_at=deadline_at,
        )
        db.add(task)
        db.flush()  # need task.id for the audit row

        # 6. INSERT the 'created' audit row (spec §7 details shape).
        db.add(TaskAuditLog(
            task_id=task.id,
            actor_user_id=actor.id,
            action="created",
            details={
                "owner_user_id": owner.id,
                "store_scope": store_scope,
                "category": category,
                "deadline_at": deadline_at.isoformat(),
                "title": title,
            },
        ))
        db.commit()
        db.refresh(task)
        return jsonify({"ok": True, "task": _task_json(task)}), 200
    finally:
        db.close()


@tasks_bp.route("/partner/tasks/<int:task_id>/reassign", methods=["POST"])
def reassign_task(task_id: int):
    """Reassign a task to a new owner. Spec §6.2 — load, enforce
    can_assign_to BEFORE the mutation, UPDATE owner + INSERT a
    'reassigned' TaskAuditLog row."""
    actor = _require_user()

    raw_new_owner = (request.form.get("new_owner_user_id") or "").strip()
    if not raw_new_owner:
        abort(400, description="new_owner_user_id is required")
    try:
        new_owner_id = int(raw_new_owner)
    except ValueError:
        abort(400, description="new_owner_user_id must be an integer")

    db = SessionLocal()
    try:
        # 1. Load the Task.
        task = db.get(Task, task_id)
        if task is None:
            abort(404, description=f"task {task_id} not found")

        # 2. Load the proposed new owner.
        new_owner = _load_active_user(db, new_owner_id)

        # 3. Enforce can_assign_to BEFORE the mutation (rule 1).
        if not can_assign_to(actor, new_owner):
            abort(403, description="you may not reassign this task to that user")

        # 4. Capture the old owner, then mutate.
        from_owner_user_id = task.owner_user_id
        task.owner_user_id = new_owner.id
        task.assigned_by_user_id = actor.id
        task.updated_at = datetime.utcnow()

        # 5. INSERT the 'reassigned' audit row (spec §7 details shape).
        db.add(TaskAuditLog(
            task_id=task.id,
            actor_user_id=actor.id,
            action="reassigned",
            details={
                "from_owner_user_id": from_owner_user_id,
                "to_owner_user_id": new_owner.id,
                "reassigned_by_user_id": actor.id,
            },
        ))
        db.commit()
        db.refresh(task)
        return jsonify({"ok": True, "task": _task_json(task)}), 200
    finally:
        db.close()
