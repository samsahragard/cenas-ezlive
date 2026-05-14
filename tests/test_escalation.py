"""Phase 2 / Block 1E — task-escalation engine tests.

Covers:
  - immediate_manager (role_hierarchy.py): the precondition spec
    deferred this helper to 1E ("1E adds it here"), so its tests live
    with the rest of 1E rather than in test_role_hierarchy.py — which
    is scoped to the pure role_hierarchy functions and uses a
    request-context fixture immediate_manager doesn't need.
  - run_escalation_scan (escalation.py): leg 1 first-tier escalation,
    leg 2 second-tier escalation + the two-tier cap, leg 3
    SalesInsight auto-expiry (import-guarded — 1F not built), the
    summary shape, idempotency.
  - POST /cron/task-escalation: the CRON_TOKEN gate.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from app.models import Task, TaskAuditLog, User
from app.services.escalation import run_escalation_scan
from app.services.role_hierarchy import immediate_manager


# ============================================================
# Helpers
# ============================================================

def _seed_user(db, uid, role="cook", store="tomball", active=True):
    u = User(
        id=uid, full_name=f"User {uid}", email=f"u{uid}@x.test",
        passcode_hash="x", permission_level=role,
        store_scope=store, active=active, first_login_done=True,
    )
    db.add(u)
    return u


def _seed_task(db, *, owner_id, store="tomball", category="vendor",
               deadline_at=None, completed_at=None,
               escalated_to_user_id=None, escalated_at=None,
               assigned_by_user_id=None, title="t"):
    t = Task(
        title=title, owner_user_id=owner_id,
        assigned_by_user_id=assigned_by_user_id or owner_id,
        store_scope=store, category=category,
        deadline_at=deadline_at or (datetime.utcnow() - timedelta(hours=1)),
        completed_at=completed_at,
        escalated_to_user_id=escalated_to_user_id,
        escalated_at=escalated_at,
    )
    db.add(t)
    return t


def _escalated_rows(db, task_id):
    return (
        db.query(TaskAuditLog)
        .filter(TaskAuditLog.task_id == task_id,
                TaskAuditLog.action == "escalated")
        .all()
    )


# ============================================================
# immediate_manager — the 1E-added role_hierarchy helper
# ============================================================

def test_immediate_manager_basic_climb_domain_preferred(db_session):
    # A kitchen cook escalates to the kitchen manager, not the FOH one.
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    _seed_user(db_session, 3, "foh_manager", "tomball")
    db_session.commit()
    mgr = immediate_manager(db_session.get(User, 1), db_session)
    assert mgr is not None and mgr.id == 2


def test_immediate_manager_domain_preference_foh(db_session):
    # ... and an FOH server escalates to the FOH manager, not the KM.
    _seed_user(db_session, 1, "server", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    _seed_user(db_session, 3, "foh_manager", "tomball")
    db_session.commit()
    mgr = immediate_manager(db_session.get(User, 1), db_session)
    assert mgr is not None and mgr.id == 3


def test_immediate_manager_partner_has_none(db_session):
    _seed_user(db_session, 1, "partner", None)
    _seed_user(db_session, 2, "gm", "tomball")
    db_session.commit()
    assert immediate_manager(db_session.get(User, 1), db_session) is None


def test_immediate_manager_store_scope_excludes_other_store(db_session):
    # A Tomball cook's only manager is at Copperfield — no coverage.
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "copperfield")
    db_session.commit()
    assert immediate_manager(db_session.get(User, 1), db_session) is None


def test_immediate_manager_store_unscoped_candidate_covers(db_session):
    # corporate is store-unscoped — covers a Tomball cook even with no
    # in-store manager.
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "copperfield")
    _seed_user(db_session, 3, "corporate", None)
    db_session.commit()
    mgr = immediate_manager(db_session.get(User, 1), db_session)
    assert mgr is not None and mgr.id == 3


def test_immediate_manager_skips_inactive(db_session):
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball", active=False)
    _seed_user(db_session, 3, "gm", "tomball")
    db_session.commit()
    mgr = immediate_manager(db_session.get(User, 1), db_session)
    assert mgr is not None and mgr.id == 3  # km inactive → climb to gm


def test_immediate_manager_lowest_tier_wins(db_session):
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    _seed_user(db_session, 3, "gm", "tomball")
    db_session.commit()
    mgr = immediate_manager(db_session.get(User, 1), db_session)
    assert mgr is not None and mgr.id == 2  # km (tier 2) over gm (tier 3)


def test_immediate_manager_id_tiebreak(db_session):
    # Two equally-qualifying kitchen managers → lowest User.id wins.
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 5, "km", "tomball")
    _seed_user(db_session, 3, "km", "tomball")
    db_session.commit()
    mgr = immediate_manager(db_session.get(User, 1), db_session)
    assert mgr is not None and mgr.id == 3


# ============================================================
# run_escalation_scan — Leg 1: first-tier escalation
# ============================================================

def test_leg1_escalates_overdue_task(db_session):
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    db_session.commit()
    t = _seed_task(db_session, owner_id=1,
                   deadline_at=datetime.utcnow() - timedelta(hours=1))
    db_session.commit()

    summary = run_escalation_scan(db_session)
    db_session.flush()

    assert t.escalated_to_user_id == 2
    assert t.escalated_at is not None
    assert summary["first_tier"] == {"scanned": 1, "escalated": 1,
                                     "no_manager": 0}
    # A freshly-escalated task is NOT also picked up by leg 2 same-run.
    assert summary["second_tier"]["escalated"] == 0

    rows = _escalated_rows(db_session, t.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor_user_id == 1   # owner is the actor (system-initiated)
    assert row.action == "escalated"
    assert row.details == {
        "escalated_to_user_id": 2,
        "tier": 1,
        "deadline_at": t.deadline_at.isoformat(),
    }


def test_leg1_skips_future_task(db_session):
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    db_session.commit()
    t = _seed_task(db_session, owner_id=1,
                   deadline_at=datetime.utcnow() + timedelta(hours=1))
    db_session.commit()

    summary = run_escalation_scan(db_session)
    assert summary["first_tier"]["scanned"] == 0
    assert t.escalated_at is None


def test_leg1_skips_completed_task(db_session):
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    db_session.commit()
    t = _seed_task(db_session, owner_id=1,
                   deadline_at=datetime.utcnow() - timedelta(hours=1),
                   completed_at=datetime.utcnow() - timedelta(minutes=5))
    db_session.commit()

    summary = run_escalation_scan(db_session)
    assert summary["first_tier"]["scanned"] == 0
    assert t.escalated_at is None


def test_leg1_skips_already_escalated_task(db_session):
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    db_session.commit()
    t = _seed_task(db_session, owner_id=1,
                   deadline_at=datetime.utcnow() - timedelta(hours=1),
                   escalated_to_user_id=2,
                   escalated_at=datetime.utcnow() - timedelta(hours=2))
    db_session.commit()

    summary = run_escalation_scan(db_session)
    assert summary["first_tier"]["scanned"] == 0


def test_leg1_no_manager_for_partner_owner(db_session):
    # A partner owns an overdue task — nobody is above them.
    _seed_user(db_session, 1, "partner", None)
    db_session.commit()
    t = _seed_task(db_session, owner_id=1, store="both",
                   deadline_at=datetime.utcnow() - timedelta(hours=1))
    db_session.commit()

    summary = run_escalation_scan(db_session)
    assert summary["first_tier"] == {"scanned": 1, "escalated": 0,
                                     "no_manager": 1}
    assert t.escalated_at is None  # left for a future scan to retry


# ============================================================
# run_escalation_scan — Leg 2: second-tier escalation + the cap
# ============================================================

def _seed_tier1_escalated(db, *, owner_id, mgr_id, escalated_hours_ago):
    """A task already first-tier-escalated to mgr_id, with its one
    'escalated' audit row — the leg-2 precondition state."""
    t = _seed_task(db, owner_id=owner_id,
                   deadline_at=datetime.utcnow() - timedelta(hours=48),
                   escalated_to_user_id=mgr_id,
                   escalated_at=datetime.utcnow()
                   - timedelta(hours=escalated_hours_ago))
    db.flush()
    db.add(TaskAuditLog(
        task_id=t.id, actor_user_id=owner_id, action="escalated",
        details={"escalated_to_user_id": mgr_id, "tier": 1,
                 "deadline_at": t.deadline_at.isoformat()},
    ))
    return t


def test_leg2_escalates_stale_tier1(db_session):
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    _seed_user(db_session, 3, "gm", "tomball")
    db_session.commit()
    t = _seed_tier1_escalated(db_session, owner_id=1, mgr_id=2,
                              escalated_hours_ago=25)
    db_session.commit()

    summary = run_escalation_scan(db_session)
    db_session.flush()

    assert t.escalated_to_user_id == 3   # climbed km → gm
    assert summary["second_tier"]["escalated"] == 1
    assert summary["second_tier"]["capped"] == 0
    rows = _escalated_rows(db_session, t.id)
    assert len(rows) == 2                # tier-1 row + new tier-2 row
    tier2 = [r for r in rows if r.details.get("tier") == 2]
    assert len(tier2) == 1
    assert tier2[0].details["escalated_to_user_id"] == 3
    assert tier2[0].actor_user_id == 1   # still the owner


def test_leg2_caps_at_two_tiers(db_session):
    # A task that already has two 'escalated' rows is capped — leg 2
    # leaves it alone, no third tier.
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    _seed_user(db_session, 3, "gm", "tomball")
    db_session.commit()
    t = _seed_tier1_escalated(db_session, owner_id=1, mgr_id=3,
                              escalated_hours_ago=25)
    db_session.flush()
    db_session.add(TaskAuditLog(
        task_id=t.id, actor_user_id=1, action="escalated",
        details={"escalated_to_user_id": 3, "tier": 2,
                 "deadline_at": t.deadline_at.isoformat()},
    ))
    db_session.commit()

    summary = run_escalation_scan(db_session)
    db_session.flush()

    assert t.escalated_to_user_id == 3   # unchanged
    assert summary["second_tier"]["escalated"] == 0
    assert summary["second_tier"]["capped"] == 1
    assert len(_escalated_rows(db_session, t.id)) == 2  # no third row


def test_leg2_skips_recent_escalation(db_session):
    # Escalated only an hour ago — not yet stale (<24h).
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    _seed_user(db_session, 3, "gm", "tomball")
    db_session.commit()
    t = _seed_tier1_escalated(db_session, owner_id=1, mgr_id=2,
                              escalated_hours_ago=1)
    db_session.commit()

    summary = run_escalation_scan(db_session)
    assert summary["second_tier"]["scanned"] == 0
    assert t.escalated_to_user_id == 2   # unchanged


def test_leg2_skips_completed_task(db_session):
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    _seed_user(db_session, 3, "gm", "tomball")
    db_session.commit()
    t = _seed_tier1_escalated(db_session, owner_id=1, mgr_id=2,
                              escalated_hours_ago=25)
    t.completed_at = datetime.utcnow() - timedelta(minutes=5)
    db_session.commit()

    summary = run_escalation_scan(db_session)
    assert summary["second_tier"]["scanned"] == 0


def test_leg2_no_manager_above_tier1(db_session):
    # The tier-1 manager is already a partner — nobody is above them.
    _seed_user(db_session, 1, "gm", "tomball")
    _seed_user(db_session, 2, "partner", None)
    db_session.commit()
    t = _seed_tier1_escalated(db_session, owner_id=1, mgr_id=2,
                              escalated_hours_ago=25)
    db_session.commit()

    summary = run_escalation_scan(db_session)
    db_session.flush()
    assert summary["second_tier"] == {"scanned": 1, "escalated": 0,
                                      "capped": 0, "no_manager": 1}
    assert t.escalated_to_user_id == 2   # unchanged
    assert len(_escalated_rows(db_session, t.id)) == 1


# ============================================================
# run_escalation_scan — Leg 3: SalesInsight auto-expiry
# ============================================================

def test_leg3_no_sales_insight_model(db_session):
    # SalesInsight is Block 1F — not built. Leg 3 is import-guarded and
    # reports itself unavailable rather than crashing.
    summary = run_escalation_scan(db_session)
    assert summary["insight_expiry"] == {"expired": 0, "available": False}


# ============================================================
# run_escalation_scan — summary shape + idempotency
# ============================================================

def test_run_escalation_scan_summary_shape(db_session):
    summary = run_escalation_scan(db_session)
    assert set(summary) == {"scanned_at", "first_tier", "second_tier",
                            "insight_expiry"}
    assert summary["first_tier"] == {"scanned": 0, "escalated": 0,
                                     "no_manager": 0}
    assert summary["second_tier"] == {"scanned": 0, "escalated": 0,
                                      "capped": 0, "no_manager": 0}
    # scanned_at is an ISO timestamp string.
    datetime.fromisoformat(summary["scanned_at"])


def test_scan_is_idempotent(db_session):
    _seed_user(db_session, 1, "cook", "tomball")
    _seed_user(db_session, 2, "km", "tomball")
    db_session.commit()
    t = _seed_task(db_session, owner_id=1,
                   deadline_at=datetime.utcnow() - timedelta(hours=1))
    db_session.commit()

    first = run_escalation_scan(db_session)
    db_session.flush()
    assert first["first_tier"]["escalated"] == 1

    second = run_escalation_scan(db_session)
    db_session.flush()
    # escalated_at is set now → task drops out of leg 1's filter.
    assert second["first_tier"]["scanned"] == 0
    assert second["first_tier"]["escalated"] == 0
    # Still exactly one 'escalated' audit row — no duplicate.
    assert len(_escalated_rows(db_session, t.id)) == 1


# ============================================================
# POST /cron/task-escalation — the CRON_TOKEN gate
# ============================================================

def test_cron_task_escalation_requires_token(monkeypatch):
    monkeypatch.setenv("CRON_TOKEN", "secret-test-token")
    os.environ.setdefault("ALLOW_DEV_SECRET", "1")
    os.environ.setdefault("SECRET_KEY", "devkey")
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    # No token → 403 (the route exists and is gated).
    assert client.post("/cron/task-escalation").status_code == 403
    # Wrong token → 403.
    bad = client.post("/cron/task-escalation",
                      headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 403
