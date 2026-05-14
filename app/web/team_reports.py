"""Team reports — Phase 2 / Block 1 / sub-block 1G (ck, 2026-05-14).

The task-based team-reports tab at /partner/team-reports/. Read-only —
no mutations — so the entire risk surface is *who can see what*.
samai's 1G spec calls this "the highest-stakes permission surface in
Phase 2"; the four gating layers below are built to that weight.

THE FOUR GATING LAYERS (1G spec §2 — all four must hold):
  1. Route access — every route carries
     @requires_permission("team_reports.view"); only partner /
     corporate / gm hold the tag, everyone else → /access-denied.
  2. Sidebar link — wrapped in has_permission('team_reports.view')
     in sidebar.html (not in this file, but part of the same gate).
  3. Store scope — every report query filters by a store scope
     DERIVED SERVER-SIDE from current_user, never read from a request
     parameter. A GM is confined to their own store; a ?store= param
     from a GM is IGNORED (not honored, not 400'd — a 400 would leak
     that the param is meaningful). Defense-in-depth: layer 1 grants
     the tab, layer 3 confines the data — the same shape as the
     251621f unassign-courier cross-check.
  4. Report-level — report #4 (per-store comparison) + the unfiltered
     cross-store view are gated by the second tag
     team_reports.view_all_stores (corporate + partner only). A GM
     holding team_reports.view still never sees report #4, and the
     cross-store query never runs for them.

THE "MISS" DEFINITION (1G spec §6 — drives personnel decisions, must
be exactly right): a task owned by an employee, deadline_at in the
window, is MISSED if escalated (escalated_at IS NOT NULL) OR completed
late (completed_at > deadline_at); CLEAN if completed_at <=
deadline_at; an open task with a future deadline is NOT COUNTED; an
open task past its deadline but not yet escalated (the <5min cron lag)
IS MISSED. A reassigned task's miss attributes to the owner AT THE
TIME THE DEADLINE PASSED — reconstructed from TaskAuditLog 'reassigned'
rows, not the current owner_user_id. See _owner_at_deadline().

1G scope boundary — NOT here: the 2H manager-tools reports
(attendance / counseling / training / maintenance) are a separate
sub-block that adds *to* this tab later. 1G is task-based reports
only, and ships no data production — it reads what 1A's tables hold.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta

from flask import Blueprint, g, render_template, request

from app.db import SessionLocal
from app.models import Task, TaskAuditLog, User
from app.services.permissions import requires_permission, has_permission

team_reports_bp = Blueprint("team_reports", __name__)


# ============================================================
# Store-scope primitives (1G spec §4)
# ============================================================
# A store_scope token → the set of physical stores it covers. Mirrors
# role_hierarchy._STORE_SETS / _store_scopes_intersect — kept
# self-contained here (proper set intersection, NOT the substring
# membership samai flagged as fragile in the 1D signal check) rather
# than importing a module-private. Shared-helper extraction is a
# noted follow-up candidate.
_STORE_SETS: dict[str, frozenset[str]] = {
    "tomball": frozenset({"tomball"}),
    "copperfield": frozenset({"copperfield"}),
    "both": frozenset({"tomball", "copperfield"}),
    "none": frozenset(),
}
# The physical stores, for report #4's per-store breakdown.
_PHYSICAL_STORES = ("tomball", "copperfield")
# Window: 30-day default, 90-day toggle (1G spec §5 / §11 Q1).
_VALID_WINDOWS = (30, 90)
_DEFAULT_WINDOW = 30


def _covers(user_scope: str | None, task_scope: str | None) -> bool:
    """True if a task scoped to task_scope is visible to a viewer
    scoped to user_scope. Proper set intersection — None / unknown →
    empty set → no intersection (fails safe-closed). A task_scope of
    'none' covers nothing, so store-agnostic tasks are not surfaced in
    a store-scoped (GM) report; partner / corporate see them via the
    unfiltered path."""
    u = _STORE_SETS.get((user_scope or "").strip().lower(), frozenset())
    t = _STORE_SETS.get((task_scope or "").strip().lower(), frozenset())
    return bool(u & t)


def _derive_store_scope(user):
    """Layer-3 store scope, DERIVED SERVER-SIDE from current_user —
    never from a request parameter (1G spec §4).

    Returns (mode, user_store):
      - ("all", None)         → partner / corporate: see every store.
      - ("store", "<token>")  → gm: confined to their own
        User.store_scope, with no override path. A ?store= param in
        the request is simply not consulted here — the scope is
        whatever this function returns, period.

    The mode is decided by the team_reports.view_all_stores tag, not
    by a role-string check: a holder of that tag gets cross-store
    visibility; a holder of only team_reports.view is store-confined.
    That keeps the gate matrix-testable (test_permission_matrix.py)
    and avoids an inline `if user.role in (...)` — exactly the
    un-matrix-tested gate the permission system was built to remove.
    """
    if has_permission("team_reports.view_all_stores"):
        return ("all", None)
    return ("store", getattr(user, "store_scope", None))


# ============================================================
# Point-in-time ownership (1G spec §6 — the subtle part)
# ============================================================
def _owner_at_deadline(task, audit_rows_for_task) -> int:
    """Who owned `task` at the moment its deadline passed.

    1G spec §6: a reassigned task's miss attributes to the owner AT
    THE TIME THE DEADLINE PASSED, not whoever owns it now. The
    ownership history lives in TaskAuditLog: the 'created' row's
    details carry the original owner_user_id; each 'reassigned' row
    (ordered by created_at) carries to_owner_user_id and the time of
    the handoff.

    Algorithm: start from the original owner; walk the 'reassigned'
    rows in chronological order; every reassignment whose created_at
    is <= the task's deadline_at moves ownership; stop at the first
    reassignment after the deadline. The result is the owner as of
    deadline_at.

    Fallbacks (defensive — a malformed/absent audit history must not
    crash a personnel report): if there is no 'created' row, fall
    back to the earliest 'reassigned' row's from_owner_user_id; if
    there is no audit history at all, fall back to the task's current
    owner_user_id (the only signal available).

    `audit_rows_for_task` is the pre-fetched list of TaskAuditLog rows
    for this task — the caller batches the query to avoid an N+1.
    """
    deadline = task.deadline_at
    created_rows = [r for r in audit_rows_for_task if r.action == "created"]
    reassigned_rows = sorted(
        (r for r in audit_rows_for_task if r.action == "reassigned"),
        key=lambda r: r.created_at,
    )

    # Original owner: prefer the 'created' row's details; else the
    # first reassignment's from_owner; else the current owner.
    owner = None
    if created_rows:
        owner = (created_rows[0].details or {}).get("owner_user_id")
    if owner is None and reassigned_rows:
        owner = (reassigned_rows[0].details or {}).get("from_owner_user_id")
    if owner is None:
        owner = task.owner_user_id

    # Walk reassignments up to (and including) the deadline.
    for r in reassigned_rows:
        if r.created_at is None:
            continue
        if r.created_at <= deadline:
            to_owner = (r.details or {}).get("to_owner_user_id")
            if to_owner is not None:
                owner = to_owner
        else:
            # reassigned_rows is sorted — once we pass the deadline,
            # every later row is also after it.
            break
    return owner


# ============================================================
# The "miss" classification (1G spec §6)
# ============================================================
def _classify(task, now: datetime) -> str:
    """Classify a task whose deadline_at falls in the report window.
    Returns "missed", "clean", or "pending" (pending = not yet
    resolvable, excluded from the denominator entirely).

      missed  — escalated (escalated_at set), OR completed late
                (completed_at > deadline_at), OR open + deadline
                passed + not yet escalated (the <5min cron lag must
                not hide a real miss).
      clean   — completed on time (completed_at <= deadline_at).
      pending — still open with a future deadline; not counted.
    """
    if task.completed_at is not None:
        return "missed" if task.completed_at > task.deadline_at else "clean"
    # Not completed.
    if task.escalated_at is not None:
        return "missed"
    if task.deadline_at < now:
        # Deadline passed, incomplete, cron hasn't escalated yet.
        return "missed"
    # Open, deadline still in the future — not yet resolvable.
    return "pending"


# ============================================================
# Report computation
# ============================================================
def _fetch_window(db, window_days: int):
    """All tasks whose deadline_at falls within the last `window_days`
    + the TaskAuditLog rows for those tasks, batched. Returns
    (tasks, audit_by_task_id)."""
    now = datetime.utcnow()
    cutoff = now - timedelta(days=window_days)
    # The window is "deadline_at within the last N days" (1G spec §5):
    # deadline_at ∈ [now − N days, now]. Past-only — a task with a
    # future deadline isn't in the window at all (spec §6: "not
    # counted"). Every fetched task is therefore resolvable; _classify
    # still handles the "pending" case defensively but it won't fire
    # for this fetch set.
    tasks = (
        db.query(Task)
        .filter(Task.deadline_at >= cutoff, Task.deadline_at <= now)
        .all()
    )
    task_ids = [t.id for t in tasks]
    audit_by_task: dict[int, list] = {tid: [] for tid in task_ids}
    if task_ids:
        for row in (db.query(TaskAuditLog)
                      .filter(TaskAuditLog.task_id.in_(task_ids))
                      .all()):
            audit_by_task.setdefault(row.task_id, []).append(row)
    return tasks, audit_by_task


def _scope_filter(tasks, mode: str, user_store: str | None):
    """Layer-3 in-memory store filter. mode 'all' → unfiltered;
    mode 'store' → only tasks whose store_scope is covered by
    user_store. Applied AFTER the window fetch so the same fetched
    set feeds both the scoped and (for report #4) the per-store
    breakdowns."""
    if mode == "all":
        return list(tasks)
    return [t for t in tasks if _covers(user_store, t.store_scope)]


def _report_miss_rate(scoped_tasks, audit_by_task, now):
    """Report 1 — per-employee task miss rate. Attributed by
    point-in-time owner-at-deadline, not current owner."""
    # owner_id → [missed, total_resolved]
    tally: dict[int, list[int]] = {}
    for t in scoped_tasks:
        verdict = _classify(t, now)
        if verdict == "pending":
            continue  # not in the denominator
        owner = _owner_at_deadline(t, audit_by_task.get(t.id, []))
        bucket = tally.setdefault(owner, [0, 0])
        bucket[1] += 1
        if verdict == "missed":
            bucket[0] += 1
    rows = []
    for owner_id, (missed, total) in tally.items():
        rows.append({
            "owner_user_id": owner_id,
            "total": total,
            "missed": missed,
            "miss_rate": round(missed / total, 4) if total else 0.0,
        })
    rows.sort(key=lambda r: (-r["miss_rate"], -r["total"]))
    return rows


def _report_response_time(scoped_tasks, now):
    """Report 2 — per-manager escalation response time. For each
    manager a task was escalated TO: latency escalated_at →
    completed_at. Median is the headline (mean is skewed by one stale
    task); also a count of still-open escalations."""
    # manager_id → {"latencies": [seconds...], "open": int}
    by_mgr: dict[int, dict] = {}
    for t in scoped_tasks:
        if t.escalated_to_user_id is None or t.escalated_at is None:
            continue
        rec = by_mgr.setdefault(
            t.escalated_to_user_id, {"latencies": [], "open": 0})
        if t.completed_at is not None:
            rec["latencies"].append(
                (t.completed_at - t.escalated_at).total_seconds())
        else:
            rec["open"] += 1
    rows = []
    for mgr_id, rec in by_mgr.items():
        lats = rec["latencies"]
        rows.append({
            "manager_user_id": mgr_id,
            "resolved_count": len(lats),
            "median_hours": round(statistics.median(lats) / 3600, 2) if lats else None,
            "mean_hours": round((sum(lats) / len(lats)) / 3600, 2) if lats else None,
            "still_open": rec["open"],
        })
    rows.sort(key=lambda r: (-(r["still_open"]),
                             -(r["median_hours"] or 0)))
    return rows


def _report_missed_categories(scoped_tasks, now):
    """Report 3 — most-frequently-missed task types. Missed tasks
    grouped by Task.category."""
    counts: dict[str, int] = {}
    for t in scoped_tasks:
        if _classify(t, now) == "missed":
            counts[t.category] = counts.get(t.category, 0) + 1
    rows = [{"category": c, "missed": n} for c, n in counts.items()]
    rows.sort(key=lambda r: -r["missed"])
    return rows


def _resolve_label_map(db, scoped_tasks, audit_by_task):
    """Best-effort {user_id: full_name} for every owner / manager id a
    report references, so the template can show names not bare ids.
    One batched query; ids with no User row fall back to "User #<id>"
    in the template."""
    ids: set[int] = set()
    for t in scoped_tasks:
        ids.add(t.owner_user_id)
        if t.escalated_to_user_id is not None:
            ids.add(t.escalated_to_user_id)
        for r in audit_by_task.get(t.id, []):
            d = r.details or {}
            for k in ("owner_user_id", "from_owner_user_id", "to_owner_user_id"):
                if d.get(k) is not None:
                    ids.add(d[k])
    if not ids:
        return {}
    rows = db.query(User).filter(User.id.in_(ids)).all()
    return {u.id: u.full_name for u in rows}


# ============================================================
# Route
# ============================================================
@team_reports_bp.route("/partner/team-reports/", methods=["GET"])
@requires_permission("team_reports.view")
def team_reports_index():
    """The team-reports tab. Reports 1–3 always (store-scoped per the
    layer-3 server-derived scope); report #4 only for holders of
    team_reports.view_all_stores — and its cross-store query never
    even runs for a GM.

    Window: 30-day default, ?window=90 toggle. Anything else → 30.
    """
    user = g.current_user  # @requires_permission guarantees this is set

    # Window — the ONLY request param this route honors. Validated to
    # the allowed set {30, 90}; anything else (including a missing or
    # garbage value) falls back to the 30-day default. A ?store= param
    # is deliberately NOT consulted anywhere in this route — layer 3:
    # store scope is server-derived from current_user, full stop.
    try:
        window = int(request.args.get("window", _DEFAULT_WINDOW))
    except (TypeError, ValueError):
        window = _DEFAULT_WINDOW
    if window not in _VALID_WINDOWS:
        window = _DEFAULT_WINDOW

    mode, user_store = _derive_store_scope(user)
    can_compare = has_permission("team_reports.view_all_stores")

    now = datetime.utcnow()
    db = SessionLocal()
    try:
        tasks, audit_by_task = _fetch_window(db, window)

        # --- reports 1–3: the layer-3 store-scoped set ---
        scoped = _scope_filter(tasks, mode, user_store)
        report1 = _report_miss_rate(scoped, audit_by_task, now)
        report2 = _report_response_time(scoped, now)
        report3 = _report_missed_categories(scoped, now)

        # --- report 4: per-store comparison, view_all_stores only ---
        # The cross-store query NEVER runs for a GM (can_compare False)
        # — not "runs then hides", does not run. Layer 4.
        report4 = None
        if can_compare:
            report4 = {}
            for store in _PHYSICAL_STORES:
                store_tasks = [t for t in tasks if _covers(store, t.store_scope)]
                r1 = _report_miss_rate(store_tasks, audit_by_task, now)
                r2 = _report_response_time(store_tasks, now)
                # Headline numbers only for the side-by-side.
                total_resolved = sum(r["total"] for r in r1)
                total_missed = sum(r["missed"] for r in r1)
                report4[store] = {
                    "total_tasks": total_resolved,
                    "missed_tasks": total_missed,
                    "miss_rate": round(total_missed / total_resolved, 4) if total_resolved else 0.0,
                    "open_escalations": sum(r["still_open"] for r in r2),
                }

        labels = _resolve_label_map(db, scoped, audit_by_task)
    finally:
        db.close()

    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    return render_template(
        "team_reports/index.html",
        active="team_reports",
        window=window,
        window_options=_VALID_WINDOWS,
        scope_mode=mode,
        scope_store=user_store,
        can_compare=can_compare,
        report1=report1,
        report2=report2,
        report3=report3,
        report4=report4,
        labels=labels,
    )


def request_window():
    """Pull ?window= off the request. Isolated so the route stays
    readable + it's trivially mockable in tests."""
    from flask import request
    return request.args.get("window", _DEFAULT_WINDOW)


def install(app):
    """Register the team-reports blueprint. Called from
    app.create_app() (mirrors the other ez*.install / register
    patterns)."""
    app.register_blueprint(team_reports_bp)
