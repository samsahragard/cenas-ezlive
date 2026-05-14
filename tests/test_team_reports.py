"""Block 1G — team-reports tab tests.

Phase 2 / Block 1 / sub-block 1G (ck, 2026-05-14). The highest-stakes
permission surface in Phase 2 — the test weight matches. Covers, per
1G spec §9:

  1. Route-level gating — the @requires_permission("team_reports.view")
     decorator is actually ON the route: partner/corporate/gm reach
     the view; manager/km/assistant_km/foh_manager/expo/driver are
     redirected to /access-denied (the decorator caught them, the
     inner view never ran). [The (role × tag) MATRIX itself is
     already covered by test_permission_matrix.py from 2255c1d, which
     auto-extends to the two new tags — this file tests that the
     decorator is wired, not that the dict is right.]
  2. Store scope (layer 3) — _derive_store_scope is server-derived
     from current_user: partner/corporate → ("all", None); gm →
     ("store", their store_scope). A ?store= param is never consulted.
  3. _covers — proper set-intersection store visibility (NOT the
     substring-membership samai flagged in the 1D signal check).
  4. Report #4 gating (layer 4) — the cross-store comparison is
     populated only for view_all_stores holders; a GM's route call
     gets report4=None and the per-store query never runs.
  5. The "miss" definition (§6) — table-driven over every case:
     on-time→clean, late→missed, escalated→missed, open-future→
     pending (not counted), open-past-not-escalated→missed.
  6. Point-in-time ownership (§6, the subtle case) — a task
     reassigned BEFORE its deadline attributes the miss to the
     owner-at-deadline (the new owner); reassigned AFTER attributes
     it to the old owner.

Run cold — in-memory SQLite via db_session. render_template is mocked
in the route tests so they exercise the real gating / scope / report
logic without full-template-render fragility (base_dashboard.html
pulls a lot of context); the template itself is covered by samai's
deploy-verify once the Render quota is restored.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app.models import Task, TaskAuditLog
from app.web import team_reports as tr


# ---- fixtures + helpers ----

@pytest.fixture(scope="session")
def app():
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _user(user_id=1, role="gm", store_scope="tomball"):
    return SimpleNamespace(
        id=user_id, full_name=f"Test {role}",
        permission_level=role, store_scope=store_scope, active=True,
    )


def _mk_task(db, *, owner_id, store_scope="tomball", category="vendor",
             deadline_offset_h=-24, completed_offset_h=None,
             escalated=False, escalated_to=None):
    """Create a Task. deadline_offset_h / completed_offset_h are hours
    relative to now (negative = past). escalated sets escalated_at."""
    now = datetime.utcnow()
    deadline = now + timedelta(hours=deadline_offset_h)
    t = Task(
        title="t", owner_user_id=owner_id, assigned_by_user_id=owner_id,
        store_scope=store_scope, category=category, deadline_at=deadline,
        escalated_to_user_id=escalated_to,
        escalated_at=(now if escalated else None),
    )
    if completed_offset_h is not None:
        t.completed_at = now + timedelta(hours=completed_offset_h)
        t.completed_by_user_id = owner_id
    db.add(t); db.commit()
    return t


# ============================================================
# 1. Route-level gating — the decorator is wired
# ============================================================

@pytest.mark.parametrize(
    "role,expect_pass",
    [
        ("partner", True), ("corporate", True), ("gm", True),
        # NOTE on "manager": it is a _LEGACY_ALIASES entry → resolves
        # to "gm" in _canonical_role, so a legacy `manager` row gets
        # gm's full permission set, INCLUDING team_reports.view → it
        # PASSES. samai's 1G spec §9 lists "manager → 403" but didn't
        # account for the alias; the denied set is really the
        # *canonical* sub-gm roles (km / assistant_km / foh_manager /
        # expo / driver), all five tested below. Flagged for samai —
        # a 1-line §9 clarification, not a code change (the alias
        # model is system-wide and correct).
        ("manager", True),
        ("km", False), ("assistant_km", False),
        ("foh_manager", False), ("expo", False), ("driver", False),
    ],
    ids=lambda v: str(v),
)
def test_route_gating_matrix(app, db_session, monkeypatch, role, expect_pass):
    """partner/corporate/gm (+ legacy 'manager'→gm) reach the view;
    the five canonical sub-gm roles are redirected to /access-denied
    by @requires_permission — the inner view never runs for them."""
    # _log_denial (on the deny path) does a delayed `from app.db import
    # SessionLocal`; the route's own SessionLocal is module-level in
    # team_reports. Patch both so neither path touches the real DB.
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.web.team_reports.SessionLocal", lambda: db_session)
    sentinel = object()
    monkeypatch.setattr(tr, "render_template",
                        lambda *a, **k: sentinel)
    with app.test_request_context("/partner/team-reports/"):
        g.current_user = _user(role=role)
        result = tr.team_reports_index()
    if expect_pass:
        # Decorator passed → inner view ran → returned our sentinel.
        assert result is sentinel
    else:
        # Decorator denied → redirect Response to /access-denied.
        assert getattr(result, "status_code", None) == 302
        assert "/access-denied" in result.location


# ============================================================
# 2. Store scope (layer 3) — server-derived, never from a param
# ============================================================

def test_derive_store_scope_partner_all(app):
    with app.test_request_context("/partner/team-reports/?store=copperfield"):
        g.current_user = _user(role="partner", store_scope=None)
        mode, store = tr._derive_store_scope(g.current_user)
    assert mode == "all" and store is None


def test_derive_store_scope_corporate_all(app):
    with app.test_request_context("/partner/team-reports/"):
        g.current_user = _user(role="corporate", store_scope=None)
        mode, store = tr._derive_store_scope(g.current_user)
    assert mode == "all"


def test_derive_store_scope_gm_confined_to_own_store(app):
    """A GM is confined to their own store_scope — and a ?store= param
    is NEVER consulted (the derivation reads current_user, full stop)."""
    with app.test_request_context("/partner/team-reports/?store=copperfield"):
        g.current_user = _user(role="gm", store_scope="tomball")
        mode, store = tr._derive_store_scope(g.current_user)
    # The ?store=copperfield is ignored — scope is the GM's own store.
    assert mode == "store" and store == "tomball"


# ============================================================
# 3. _covers — proper set-intersection store visibility
# ============================================================

@pytest.mark.parametrize("user_store,task_scope,expected", [
    ("tomball", "tomball", True),
    ("tomball", "both", True),
    ("tomball", "copperfield", False),
    ("tomball", "none", False),
    ("copperfield", "copperfield", True),
    ("copperfield", "both", True),
    ("copperfield", "tomball", False),
    ("both", "tomball", True),
    ("both", "copperfield", True),
    (None, "tomball", False),       # unknown user scope → fails closed
    ("tomball", None, False),       # unknown task scope → fails closed
])
def test_covers(user_store, task_scope, expected):
    assert tr._covers(user_store, task_scope) is expected


# ============================================================
# 4. Report #4 gating (layer 4)
# ============================================================

def test_report4_populated_for_partner(app, db_session, monkeypatch):
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.web.team_reports.SessionLocal", lambda: db_session)
    captured = {}
    monkeypatch.setattr(tr, "render_template",
                        lambda *a, **k: captured.update(k) or "ok")
    with app.test_request_context("/partner/team-reports/"):
        g.current_user = _user(role="partner", store_scope=None)
        tr.team_reports_index()
    assert captured["can_compare"] is True
    assert captured["report4"] is not None
    assert set(captured["report4"].keys()) == {"tomball", "copperfield"}


def test_report4_none_for_gm(app, db_session, monkeypatch):
    """A GM holds team_reports.view but NOT team_reports.view_all_stores
    — report4 is None and the cross-store query never runs."""
    monkeypatch.setattr("app.db.SessionLocal", lambda: db_session)
    monkeypatch.setattr("app.web.team_reports.SessionLocal", lambda: db_session)
    captured = {}
    monkeypatch.setattr(tr, "render_template",
                        lambda *a, **k: captured.update(k) or "ok")
    with app.test_request_context("/partner/team-reports/"):
        g.current_user = _user(role="gm", store_scope="tomball")
        tr.team_reports_index()
    assert captured["can_compare"] is False
    assert captured["report4"] is None


# ============================================================
# 5. The "miss" definition (§6) — table-driven
# ============================================================

@pytest.mark.parametrize("desc,kwargs,expected", [
    ("completed on time → clean",
     dict(deadline_offset_h=-24, completed_offset_h=-25), "clean"),
    ("completed late → missed",
     dict(deadline_offset_h=-24, completed_offset_h=-1), "missed"),
    ("escalated → missed",
     dict(deadline_offset_h=-24, escalated=True), "missed"),
    ("open, deadline passed, not yet escalated → missed",
     dict(deadline_offset_h=-1), "missed"),
    ("open, deadline in the future → pending (not counted)",
     dict(deadline_offset_h=+48), "pending"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_miss_classification(db_session, desc, kwargs, expected):
    task = _mk_task(db_session, owner_id=1, **kwargs)
    now = datetime.utcnow()
    assert tr._classify(task, now) == expected, desc


# ============================================================
# 6. Point-in-time ownership (§6 — the subtle case)
# ============================================================

def test_owner_at_deadline_reassigned_before_deadline(db_session):
    """Created for A, reassigned to B BEFORE the deadline passed, then
    missed → the miss attributes to B (owner when the deadline passed),
    not A."""
    now = datetime.utcnow()
    deadline = now - timedelta(hours=2)
    task = Task(
        title="t", owner_user_id=2,  # current owner = B
        assigned_by_user_id=1, store_scope="tomball", category="vendor",
        deadline_at=deadline, escalated_at=now,
    )
    db_session.add(task); db_session.commit()
    # created (owner A=1) at deadline-10h; reassigned A→B at deadline-5h.
    audit = [
        TaskAuditLog(task_id=task.id, actor_user_id=1, action="created",
                     details={"owner_user_id": 1},
                     created_at=deadline - timedelta(hours=10)),
        TaskAuditLog(task_id=task.id, actor_user_id=1, action="reassigned",
                     details={"from_owner_user_id": 1, "to_owner_user_id": 2},
                     created_at=deadline - timedelta(hours=5)),
    ]
    for a in audit:
        db_session.add(a)
    db_session.commit()
    # Reassignment happened BEFORE the deadline → owner-at-deadline = B (2).
    assert tr._owner_at_deadline(task, audit) == 2


def test_owner_at_deadline_reassigned_after_deadline(db_session):
    """Created for A, reassigned to B AFTER the deadline already passed
    → the miss still attributes to A (owner when the deadline passed),
    not the post-hoc new owner B."""
    now = datetime.utcnow()
    deadline = now - timedelta(hours=10)
    task = Task(
        title="t", owner_user_id=2,  # current owner = B
        assigned_by_user_id=1, store_scope="tomball", category="vendor",
        deadline_at=deadline, escalated_at=now,
    )
    db_session.add(task); db_session.commit()
    # created (owner A=1) at deadline-5h; reassigned A→B at deadline+2h.
    audit = [
        TaskAuditLog(task_id=task.id, actor_user_id=1, action="created",
                     details={"owner_user_id": 1},
                     created_at=deadline - timedelta(hours=5)),
        TaskAuditLog(task_id=task.id, actor_user_id=1, action="reassigned",
                     details={"from_owner_user_id": 1, "to_owner_user_id": 2},
                     created_at=deadline + timedelta(hours=2)),
    ]
    for a in audit:
        db_session.add(a)
    db_session.commit()
    # Reassignment happened AFTER the deadline → owner-at-deadline = A (1).
    assert tr._owner_at_deadline(task, audit) == 1


def test_owner_at_deadline_no_audit_falls_back_to_current(db_session):
    """No audit history at all → fall back to the task's current
    owner_user_id (the only signal available — defensive, never
    crashes a personnel report)."""
    now = datetime.utcnow()
    task = Task(
        title="t", owner_user_id=7, assigned_by_user_id=7,
        store_scope="tomball", category="vendor",
        deadline_at=now - timedelta(hours=2), escalated_at=now,
    )
    db_session.add(task); db_session.commit()
    assert tr._owner_at_deadline(task, []) == 7


# ============================================================
# 7. Report computation — attribution + median
# ============================================================

def test_miss_rate_attributes_by_point_in_time_owner(db_session):
    """The miss-rate report attributes a reassigned task's miss to the
    owner-at-deadline, not the current owner — end-to-end through
    _report_miss_rate, not just _owner_at_deadline in isolation."""
    now = datetime.utcnow()
    deadline = now - timedelta(hours=2)
    task = Task(
        title="t", owner_user_id=2, assigned_by_user_id=1,
        store_scope="tomball", category="vendor",
        deadline_at=deadline, escalated_at=now,
    )
    db_session.add(task); db_session.commit()
    audit_rows = [
        TaskAuditLog(task_id=task.id, actor_user_id=1, action="created",
                     details={"owner_user_id": 1},
                     created_at=deadline - timedelta(hours=10)),
        TaskAuditLog(task_id=task.id, actor_user_id=1, action="reassigned",
                     details={"from_owner_user_id": 1, "to_owner_user_id": 2},
                     created_at=deadline - timedelta(hours=5)),
    ]
    for a in audit_rows:
        db_session.add(a)
    db_session.commit()
    rows = tr._report_miss_rate([task], {task.id: audit_rows}, now)
    # The single missed task is attributed to B (2), not A (1).
    assert len(rows) == 1
    assert rows[0]["owner_user_id"] == 2
    assert rows[0]["missed"] == 1
    assert rows[0]["miss_rate"] == 1.0


def test_response_time_reports_median(db_session):
    """_report_response_time reports median (the headline) + mean +
    still-open count. Median resists a single stale outlier."""
    now = datetime.utcnow()
    mgr_id = 50
    # Three escalated-and-completed tasks: latencies 1h, 2h, 9h.
    # median = 2h, mean = 4h — the test asserts both so the
    # median-not-mean headline choice is locked.
    for lat_h in (1, 2, 9):
        esc = now - timedelta(hours=20)
        t = Task(
            title="t", owner_user_id=1, assigned_by_user_id=1,
            store_scope="tomball", category="vendor",
            deadline_at=now - timedelta(hours=22),
            escalated_to_user_id=mgr_id, escalated_at=esc,
            completed_at=esc + timedelta(hours=lat_h),
            completed_by_user_id=mgr_id,
        )
        db_session.add(t)
    # One still-open escalation.
    db_session.add(Task(
        title="t", owner_user_id=1, assigned_by_user_id=1,
        store_scope="tomball", category="vendor",
        deadline_at=now - timedelta(hours=22),
        escalated_to_user_id=mgr_id, escalated_at=now - timedelta(hours=5),
    ))
    db_session.commit()
    all_tasks = db_session.query(Task).all()
    rows = tr._report_response_time(all_tasks, now)
    assert len(rows) == 1
    r = rows[0]
    assert r["manager_user_id"] == mgr_id
    assert r["resolved_count"] == 3
    assert r["median_hours"] == 2.0
    assert r["mean_hours"] == 4.0
    assert r["still_open"] == 1


def test_scope_filter_confines_gm_to_own_store(db_session):
    """_scope_filter in 'store' mode drops tasks outside the GM's
    store; in 'all' mode it passes everything through."""
    now = datetime.utcnow()
    tom = _mk_task(db_session, owner_id=1, store_scope="tomball")
    cop = _mk_task(db_session, owner_id=2, store_scope="copperfield")
    both = _mk_task(db_session, owner_id=3, store_scope="both")
    allt = db_session.query(Task).all()
    # GM scoped to tomball sees tomball + both, not copperfield.
    scoped = tr._scope_filter(allt, "store", "tomball")
    ids = {t.id for t in scoped}
    assert tom.id in ids and both.id in ids and cop.id not in ids
    # 'all' mode passes everything.
    assert len(tr._scope_filter(allt, "all", None)) == 3
