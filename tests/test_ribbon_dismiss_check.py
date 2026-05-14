"""Block 1D — ribbon X/Check endpoint tests.

Phase 2 / Block 1 / sub-block 1D (ck, 2026-05-14). Covers the
dismiss + check handlers in app/web/ribbon_routes.py:

  DISMISS (the X — self-scoped, light eligibility):
    - task / signal / scheduled_event dismiss writes a
      RibbonItemDismissal with the exact (user_id, item_type, item_id,
      dismiss_day=today) tuple 1C's _exclude_dismissed reads against
    - idempotent: a second dismiss the same day is a 200 no-op, no
      second row (the uq constraint is the backstop, the
      check-then-insert is the happy path)
    - unknown item_type → 400; non-existent item → 404;
      unauthenticated → 401

  CHECK (the ✓ — high blast radius, full per-type audience check):
    - task: owner / escalated-to / partner may complete → sets
      completed_at + completed_by_user_id + writes TaskAuditLog
      "completed"; a non-owner non-escalated non-partner → 403;
      already-complete → 200 no-op, no double-audit
    - signal: in-audience → stamps acknowledged_by/at + writes
      SignalAck; out-of-audience → 403
    - scheduled_event → 403 (not checkable — can_check is always False)
    - sales_insight → 503 (Block 1F not built — import-guarded)
    - unknown item_type → 400; non-existent → 404; unauthenticated → 401

Run cold — in-memory SQLite via the db_session fixture. g.current_user
is a SimpleNamespace (the endpoints only read .id / .permission_level
/ .store_scope); the model rows carry plain integer ids so no real
User rows are needed (SQLite doesn't enforce the FKs here, matching
the test_ribbon_component.py pattern).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app.models import (
    RibbonItemDismissal, Task, TaskAuditLog, Signal, SignalAck,
    ScheduledEvent,
)
from app.web.ribbon_routes import ribbon_dismiss, ribbon_check


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


def _call(app, db_session, monkeypatch, view_fn, item_type, item_id, user):
    """Invoke ribbon_dismiss / ribbon_check directly. Returns
    (response, status). Patches app.web.ribbon_routes.SessionLocal —
    that module does `from app.db import SessionLocal` at import time,
    so the name lives in the ribbon_routes namespace; patching
    app.db.SessionLocal would not redirect the endpoint's lookup.

    NOTE the endpoint's `finally: db.close()` closes the session it was
    handed. In the real app that's a fresh per-request session; in the
    test it's the shared db_session, so close() expunges every object.
    That's a test-harness artifact, not a bug — the assertion side must
    RE-QUERY rows by id after _call (db_session.get(Model, id)), never
    .refresh() a pre-call instance (it's detached post-close)."""
    monkeypatch.setattr(
        "app.web.ribbon_routes.SessionLocal", lambda: db_session)
    verb = "dismiss" if view_fn is ribbon_dismiss else "check"
    with app.test_request_context(
        f"/partner/ribbon/{verb}/{item_type}/{item_id}", method="POST",
    ):
        if user is not None:
            g.current_user = user
        result = view_fn(item_type, item_id)
    if isinstance(result, tuple):
        return result[0], result[1]
    return result, result.status_code


def _mk_task(db, owner_id=1, **over):
    t = Task(
        title=over.get("title", "Place SPECS order"),
        description=over.get("description"),
        owner_user_id=owner_id,
        assigned_by_user_id=over.get("assigned_by_user_id", owner_id),
        store_scope=over.get("store_scope", "tomball"),
        category=over.get("category", "vendor"),
        deadline_at=over.get("deadline_at", datetime.utcnow() + timedelta(hours=4)),
        escalated_to_user_id=over.get("escalated_to_user_id"),
        completed_at=over.get("completed_at"),
        completed_by_user_id=over.get("completed_by_user_id"),
    )
    db.add(t); db.commit()
    return t


def _mk_signal(db, **over):
    s = Signal(
        rule_name=over.get("rule_name", "prep_behind"),
        severity=over.get("severity", "warn"),
        store_id=over.get("store_id"),
        subject_id=over.get("subject_id"),
        subject_label=over.get("subject_label", "Tomball prep"),
        action_text=over.get("action_text", "Check the line"),
        surfaces=over.get("surfaces", []),
        audience_roles=over.get("audience_roles", []),
    )
    db.add(s); db.commit()
    return s


def _mk_event(db, **over):
    e = ScheduledEvent(
        store=over.get("store", "tomball"),
        category=over.get("category", "catering"),
        title=over.get("title", "Smith wedding 80pax"),
        scheduled_at=over.get("scheduled_at", datetime.utcnow() + timedelta(days=1)),
        status=over.get("status", "scheduled"),
    )
    db.add(e); db.commit()
    return e


# ============================================================
# DISMISS
# ============================================================

def test_dismiss_task_writes_dismissal_row(app, db_session, monkeypatch):
    task = _mk_task(db_session, owner_id=42)
    user = _user(user_id=42)
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_dismiss, "task", task.id, user)
    assert status == 200
    data = resp.get_json()
    assert data["ok"] is True and data["dismissed"] is True
    assert data["already"] is False
    rows = db_session.query(RibbonItemDismissal).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.user_id == 42
    assert r.item_type == "task"
    assert r.item_id == task.id
    # The exact string 1C's _exclude_dismissed compares against.
    assert r.dismiss_day == date.today().isoformat()
    assert r.dismissed_at is not None


def test_dismiss_signal_and_scheduled_event(app, db_session, monkeypatch):
    sig = _mk_signal(db_session)
    evt = _mk_event(db_session)
    user = _user(user_id=7)
    r1, s1 = _call(app, db_session, monkeypatch,
                   ribbon_dismiss, "signal", sig.id, user)
    r2, s2 = _call(app, db_session, monkeypatch,
                   ribbon_dismiss, "scheduled_event", evt.id, user)
    assert s1 == 200 and r1.get_json()["ok"] is True
    assert s2 == 200 and r2.get_json()["ok"] is True
    rows = db_session.query(RibbonItemDismissal).all()
    types = sorted(r.item_type for r in rows)
    assert types == ["scheduled_event", "signal"]


def test_dismiss_idempotent_same_day(app, db_session, monkeypatch):
    task = _mk_task(db_session, owner_id=5)
    user = _user(user_id=5)
    r1, s1 = _call(app, db_session, monkeypatch,
                   ribbon_dismiss, "task", task.id, user)
    r2, s2 = _call(app, db_session, monkeypatch,
                   ribbon_dismiss, "task", task.id, user)
    assert s1 == 200 and r1.get_json()["already"] is False
    assert s2 == 200 and r2.get_json()["already"] is True
    # Second dismiss is a no-op — still exactly one row.
    assert db_session.query(RibbonItemDismissal).count() == 1


def test_dismiss_unknown_item_type_400(app, db_session, monkeypatch):
    user = _user()
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_dismiss, "not_a_type", 1, user)
    assert status == 400
    assert resp.get_json()["ok"] is False
    assert db_session.query(RibbonItemDismissal).count() == 0


def test_dismiss_nonexistent_item_404(app, db_session, monkeypatch):
    user = _user()
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_dismiss, "task", 99999, user)
    assert status == 404
    # No dismissal row for a garbage id.
    assert db_session.query(RibbonItemDismissal).count() == 0


def test_dismiss_unauthenticated_401(app, db_session, monkeypatch):
    task = _mk_task(db_session)
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_dismiss, "task", task.id, user=None)
    assert status == 401
    assert db_session.query(RibbonItemDismissal).count() == 0


# ============================================================
# CHECK — task
# ============================================================

def test_check_task_by_owner_completes_and_audits(app, db_session, monkeypatch):
    task = _mk_task(db_session, owner_id=10)
    task_id = task.id
    user = _user(user_id=10)
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "task", task_id, user)
    assert status == 200
    assert resp.get_json()["checked"] is True
    fresh = db_session.get(Task, task_id)
    assert fresh.completed_at is not None
    assert fresh.completed_by_user_id == 10
    audits = (db_session.query(TaskAuditLog)
              .filter_by(task_id=task_id, action="completed").all())
    assert len(audits) == 1
    assert audits[0].actor_user_id == 10


def test_check_task_by_escalated_to(app, db_session, monkeypatch):
    task = _mk_task(db_session, owner_id=10, escalated_to_user_id=20)
    task_id = task.id
    manager = _user(user_id=20, role="gm")
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "task", task_id, manager)
    assert status == 200
    fresh = db_session.get(Task, task_id)
    assert fresh.completed_by_user_id == 20


def test_check_task_by_partner(app, db_session, monkeypatch):
    task = _mk_task(db_session, owner_id=10)
    task_id = task.id
    partner = _user(user_id=99, role="partner", store_scope=None)
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "task", task_id, partner)
    assert status == 200
    fresh = db_session.get(Task, task_id)
    assert fresh.completed_by_user_id == 99


def test_check_task_by_non_owner_403(app, db_session, monkeypatch):
    task = _mk_task(db_session, owner_id=10)
    task_id = task.id
    other = _user(user_id=55, role="gm")  # not owner, not escalated, not partner
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "task", task_id, other)
    assert status == 403
    fresh = db_session.get(Task, task_id)
    assert fresh.completed_at is None  # not completed
    assert db_session.query(TaskAuditLog).count() == 0  # not audited


def test_check_task_already_complete_noop(app, db_session, monkeypatch):
    task = _mk_task(db_session, owner_id=10,
                    completed_at=datetime.utcnow(), completed_by_user_id=10)
    user = _user(user_id=10)
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "task", task.id, user)
    assert status == 200
    assert resp.get_json()["already"] is True
    # No second audit row for an already-complete task.
    assert db_session.query(TaskAuditLog).count() == 0


# ============================================================
# CHECK — signal
# ============================================================

def test_check_signal_in_audience_acks(app, db_session, monkeypatch):
    sig = _mk_signal(db_session, audience_roles=["gm", "manager"])
    sig_id = sig.id
    user = _user(user_id=8, role="gm")
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "signal", sig_id, user)
    assert status == 200
    fresh = db_session.get(Signal, sig_id)
    assert fresh.acknowledged_by == 8
    assert fresh.acknowledged_at is not None
    acks = db_session.query(SignalAck).filter_by(signal_id=sig_id).all()
    assert len(acks) == 1
    assert acks[0].user_id == 8


def test_check_signal_empty_audience_visible_to_all(app, db_session, monkeypatch):
    # audience_roles=[] → visible to everyone, so any role can check it.
    sig = _mk_signal(db_session, audience_roles=[])
    sig_id = sig.id
    user = _user(user_id=8, role="expo")
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "signal", sig_id, user)
    assert status == 200
    fresh = db_session.get(Signal, sig_id)
    assert fresh.acknowledged_by == 8


def test_check_signal_out_of_audience_403(app, db_session, monkeypatch):
    sig = _mk_signal(db_session, audience_roles=["partner", "corporate"])
    sig_id = sig.id
    user = _user(user_id=8, role="expo")
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "signal", sig_id, user)
    assert status == 403
    fresh = db_session.get(Signal, sig_id)
    assert fresh.acknowledged_at is None
    assert db_session.query(SignalAck).count() == 0


def test_check_signal_wrong_store_403(app, db_session, monkeypatch):
    sig = _mk_signal(db_session, audience_roles=["gm"], store_id="copperfield")
    sig_id = sig.id
    user = _user(user_id=8, role="gm", store_scope="tomball")
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "signal", sig_id, user)
    assert status == 403
    fresh = db_session.get(Signal, sig_id)
    assert fresh.acknowledged_at is None


# ============================================================
# CHECK — scheduled_event / sales_insight / error paths
# ============================================================

def test_check_scheduled_event_403(app, db_session, monkeypatch):
    """You don't 'complete' a scheduled event — can_check is always
    False for it, so the check endpoint refuses."""
    evt = _mk_event(db_session)
    user = _user(user_id=8, role="gm")
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "scheduled_event", evt.id, user)
    assert status == 403


def test_check_sales_insight_503_until_1f(app, db_session, monkeypatch):
    """SalesInsight is Block 1F — not built. The check endpoint
    import-guards and returns 503 until 1F lands."""
    user = _user(user_id=8, role="gm")
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "sales_insight", 1, user)
    # 503 if SalesInsight model doesn't import yet; if 1F has landed by
    # the time this runs it'd be 404 (no such insight) — either is a
    # clean non-500 refusal, which is the property under test.
    assert status in (503, 404)
    assert resp.get_json()["ok"] is False


def test_check_unknown_item_type_400(app, db_session, monkeypatch):
    user = _user()
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "bogus", 1, user)
    assert status == 400


def test_check_nonexistent_item_404(app, db_session, monkeypatch):
    user = _user()
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "task", 99999, user)
    assert status == 404


def test_check_unauthenticated_401(app, db_session, monkeypatch):
    task = _mk_task(db_session)
    resp, status = _call(app, db_session, monkeypatch,
                         ribbon_check, "task", task.id, user=None)
    assert status == 401
    db_session.refresh(task)
    assert task.completed_at is None
