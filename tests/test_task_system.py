"""Phase 2 / Block 1A — task system tests.

Per the Block 1A spec §8:

  - Model: Task / TaskAuditLog / RibbonItemDismissal round-trip;
    TaskAuditLog before_delete raises RuntimeError /append-only/;
    FK ondelete declarations assert correctly; the
    uq_ribbon_dismissal_per_day unique constraint rejects a duplicate.
  - can_assign_to matrix: parameterized over (actor_role × target_role
    × same/cross store) against the §5.2 rules, plus the explicit
    self-assignment case for every role.
  - Routes: create allowed → 200 + Task + 'created' audit; create
    disallowed → 403 + no Task (audience-eligibility-before-mutation);
    reassign allowed → 200 + owner changed + 'reassigned' audit with
    correct from/to; reassign disallowed → 403 + Task unchanged;
    invalid store_scope / category / deadline_at → 400.
  - Audit emission: the §7 details JSON shape for both 1A actions.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

from app.models import (
    Task,
    TaskAuditLog,
    RibbonItemDismissal,
    User,
    _VALID_STORE_SCOPES,
    _VALID_CATEGORIES,
)
from app.services.role_hierarchy import (
    ROLE_TIER,
    _ROLE_DOMAIN,
    can_assign_to,
)


# ============================================================
# Model tests
# ============================================================

def _seed_user(db, uid, role="gm", store_scope="tomball"):
    u = User(
        id=uid, full_name=f"User {uid}", email=f"u{uid}@x.test",
        passcode_hash="x", permission_level=role,
        store_scope=store_scope, active=True, first_login_done=True,
    )
    db.add(u)
    return u


def test_task_roundtrips(db_session):
    _seed_user(db_session, 1, "gm")
    _seed_user(db_session, 2, "cook")
    db_session.commit()
    t = Task(
        title="SPECS liquor order",
        description="Place by 5pm with Andres' vendor",
        owner_user_id=2, assigned_by_user_id=1,
        store_scope="tomball", category="vendor",
        deadline_at=datetime(2026, 5, 20, 17, 0),
    )
    db_session.add(t)
    db_session.commit()

    row = db_session.query(Task).one()
    assert row.title == "SPECS liquor order"
    assert row.owner_user_id == 2
    assert row.assigned_by_user_id == 1
    assert row.store_scope == "tomball"
    assert row.category == "vendor"
    assert row.completed_at is None
    assert row.escalated_at is None
    assert row.created_at is not None
    assert row.updated_at is not None


def test_task_audit_log_roundtrips(db_session):
    _seed_user(db_session, 1, "gm")
    _seed_user(db_session, 2, "cook")
    db_session.commit()
    t = Task(title="x", owner_user_id=2, assigned_by_user_id=1,
             store_scope="tomball", category="general",
             deadline_at=datetime(2026, 5, 20, 17, 0))
    db_session.add(t)
    db_session.flush()
    log = TaskAuditLog(
        task_id=t.id, actor_user_id=1, action="created",
        details={"owner_user_id": 2, "title": "x"},
    )
    db_session.add(log)
    db_session.commit()

    row = db_session.query(TaskAuditLog).one()
    assert row.task_id == t.id
    assert row.actor_user_id == 1
    assert row.action == "created"
    assert row.details == {"owner_user_id": 2, "title": "x"}
    assert row.created_at is not None


def test_ribbon_item_dismissal_roundtrips(db_session):
    _seed_user(db_session, 1, "gm")
    db_session.commit()
    d = RibbonItemDismissal(
        user_id=1, item_type="task", item_id=42,
        dismiss_day="2026-05-14",
    )
    db_session.add(d)
    db_session.commit()

    row = db_session.query(RibbonItemDismissal).one()
    assert row.user_id == 1
    assert row.item_type == "task"
    assert row.item_id == 42
    assert row.dismiss_day == "2026-05-14"
    assert row.dismissed_at is not None


def test_task_audit_log_is_append_only(db_session):
    _seed_user(db_session, 1, "gm")
    _seed_user(db_session, 2, "cook")
    db_session.commit()
    t = Task(title="x", owner_user_id=2, assigned_by_user_id=1,
             store_scope="tomball", category="general",
             deadline_at=datetime(2026, 5, 20, 17, 0))
    db_session.add(t)
    db_session.flush()
    log = TaskAuditLog(task_id=t.id, actor_user_id=1, action="created")
    db_session.add(log)
    db_session.commit()

    with pytest.raises(RuntimeError, match="append-only"):
        db_session.delete(log)
        db_session.flush()


def test_fk_ondelete_declarations():
    """RESTRICT on owner/assigned_by/task_id/actor; SET NULL on
    completed_by/escalated_to; CASCADE on dismissal.user_id. SQLite's
    in-memory engine doesn't enforce FKs without PRAGMA, so verify the
    declared constraint rather than runtime behavior — same shape as
    the BriefFeedback model tests."""
    def _fk(table, col):
        return next(
            f for f in table.foreign_keys
            if f.parent.name == col
        )

    assert _fk(Task.__table__, "owner_user_id").ondelete == "RESTRICT"
    assert _fk(Task.__table__, "assigned_by_user_id").ondelete == "RESTRICT"
    assert _fk(Task.__table__, "completed_by_user_id").ondelete == "SET NULL"
    assert _fk(Task.__table__, "escalated_to_user_id").ondelete == "SET NULL"
    assert _fk(TaskAuditLog.__table__, "task_id").ondelete == "RESTRICT"
    assert _fk(TaskAuditLog.__table__, "actor_user_id").ondelete == "RESTRICT"
    assert _fk(RibbonItemDismissal.__table__, "user_id").ondelete == "CASCADE"


def test_ribbon_dismissal_unique_per_day(db_session):
    """uq_ribbon_dismissal_per_day rejects a duplicate (same user,
    item_type, item_id, dismiss_day) — this is what makes 1D's dismiss
    endpoint naturally idempotent."""
    from sqlalchemy.exc import IntegrityError
    _seed_user(db_session, 1, "gm")
    db_session.commit()
    db_session.add(RibbonItemDismissal(
        user_id=1, item_type="task", item_id=7, dismiss_day="2026-05-14"))
    db_session.commit()
    db_session.add(RibbonItemDismissal(
        user_id=1, item_type="task", item_id=7, dismiss_day="2026-05-14"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    # A different day for the same item is allowed.
    db_session.add(RibbonItemDismissal(
        user_id=1, item_type="task", item_id=7, dismiss_day="2026-05-15"))
    db_session.commit()
    assert db_session.query(RibbonItemDismissal).count() == 2


# ============================================================
# can_assign_to matrix
# ============================================================

_ALL_ROLES = sorted(ROLE_TIER.keys())
_STORE_UNSCOPED = {"partner", "corporate", "corporate_chef", "prep_manager"}


class _FakeUser:
    """Lightweight User stand-in — can_assign_to reads only .id,
    .permission_level, .store_scope."""
    def __init__(self, uid, role, store_scope):
        self.id = uid
        self.permission_level = role
        self.store_scope = store_scope


def _expected_can_assign(actor_role, target_role, same_store) -> bool:
    """The §5.2 / §5.3 rules, encoded independently of can_assign_to's
    implementation (derived from the spec table, not copied from the
    function under test):
      - strictly downward only (a_tier > t_tier), never to a peer
      - manager-tier actors (tier 2) assign ONLY to hourly tier (1),
        and only within their own domain
      - store-scoped actors (not in _STORE_UNSCOPED) need a store match
    Self-assignment is tested separately — this covers actor != target.
    """
    a_tier = ROLE_TIER.get(actor_role, 0)
    t_tier = ROLE_TIER.get(target_role, 0)
    if a_tier == 0:
        return False
    if a_tier <= t_tier:
        return False
    if a_tier == 2:
        if t_tier != 1:
            return False
        if _ROLE_DOMAIN[actor_role] != _ROLE_DOMAIN[target_role]:
            return False
    if actor_role not in _STORE_UNSCOPED and not same_store:
        return False
    return True


@pytest.mark.parametrize("actor_role", _ALL_ROLES)
@pytest.mark.parametrize("target_role", _ALL_ROLES)
@pytest.mark.parametrize("same_store", [True, False],
                         ids=["store=same", "store=cross"])
def test_can_assign_to_matrix(actor_role, target_role, same_store):
    """(actor_role × target_role × same/cross store) — assert
    can_assign_to matches the spec-derived expectation. Distinct
    user ids so this is never the self-assignment short-circuit."""
    actor = _FakeUser(1, actor_role, "tomball")
    target = _FakeUser(
        2, target_role, "tomball" if same_store else "copperfield")
    actual = can_assign_to(actor, target)
    expected = _expected_can_assign(actor_role, target_role, same_store)
    assert actual is expected, (
        f"can_assign_to(actor={actor_role}, target={target_role}, "
        f"store={'same' if same_store else 'cross'}) = {actual}, "
        f"expected {expected}")


@pytest.mark.parametrize("role", _ALL_ROLES)
def test_can_assign_to_self_always_true(role):
    """Every role — including hourly tier — may self-assign. Same
    user id on both sides short-circuits to True before any tier or
    store check."""
    u = _FakeUser(1, role, "tomball")
    assert can_assign_to(u, u) is True


def test_can_assign_to_unknown_actor_role_denied():
    actor = _FakeUser(1, "not_a_real_role", "tomball")
    target = _FakeUser(2, "cook", "tomball")
    assert can_assign_to(actor, target) is False


# ============================================================
# Route tests
# ============================================================

@pytest.fixture
def app_with_users(db_session, monkeypatch):
    """Flask app with the in-memory db_session bound + role-varied
    users seeded. Yields (app, client_for, db). client_for(uid)
    returns a test client with a keypad-user session for that uid."""
    # partner(1), gm-tomball(2), cook-tomball(3), cook-copperfield(4),
    # foh_manager-tomball(5), km-tomball(6)
    users = [
        (1, "partner", None),
        (2, "gm", "tomball"),
        (3, "cook", "tomball"),
        (4, "cook", "copperfield"),
        (5, "foh_manager", "tomball"),
        (6, "km", "tomball"),
    ]
    for uid, role, scope in users:
        db_session.add(User(
            id=uid, full_name=f"User {uid}", email=f"u{uid}@x.test",
            passcode_hash="x", permission_level=role,
            store_scope=scope, active=True, first_login_done=True))
    db_session.commit()

    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    import app.db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)
    import app.web.tasks as tasks_mod
    monkeypatch.setattr(tasks_mod, "SessionLocal", lambda: db_session)

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True

    def _client_for(user_id: int):
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["auth_ok"] = True
            sess["user_id"] = user_id
            sess["user_session_version"] = 1
        return c

    yield app, _client_for, db_session


_FUTURE = (datetime.utcnow() + timedelta(days=3)).isoformat()


def test_create_task_allowed_assignment(app_with_users):
    """gm(2) → cook(3), same store: 200 + Task row + 'created' audit."""
    app, client_for, db = app_with_users
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "Prep the salsa station",
        "store_scope": "tomball", "category": "general",
        "deadline_at": _FUTURE,
    })
    assert r.status_code == 200, r.data
    task_id = r.get_json()["task"]["id"]
    task = db.get(Task, task_id)
    assert task.owner_user_id == 3
    assert task.assigned_by_user_id == 2
    audit = db.query(TaskAuditLog).filter_by(task_id=task_id).all()
    assert len(audit) == 1
    assert audit[0].action == "created"


def test_create_task_disallowed_assignment_writes_nothing(app_with_users):
    """cook(3) → gm(2): can_assign_to False → 403 + NO Task row
    (audience-eligibility-before-mutation)."""
    app, client_for, db = app_with_users
    before = db.query(Task).count()
    r = client_for(3).post("/partner/tasks/create", data={
        "owner_user_id": "2", "title": "Reassign yourself upward",
        "store_scope": "tomball", "category": "general",
        "deadline_at": _FUTURE,
    })
    assert r.status_code == 403
    assert db.query(Task).count() == before  # nothing written
    assert db.query(TaskAuditLog).count() == 0


def test_create_task_self_assignment_allowed(app_with_users):
    """cook(3) → cook(3): self-assignment always allowed."""
    app, client_for, db = app_with_users
    r = client_for(3).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "Restock my station",
        "store_scope": "tomball", "category": "general",
        "deadline_at": _FUTURE,
    })
    assert r.status_code == 200
    assert db.get(Task, r.get_json()["task"]["id"]).owner_user_id == 3


def test_create_task_default_owner_is_self(app_with_users):
    """No owner_user_id in the form → defaults to current_user."""
    app, client_for, db = app_with_users
    r = client_for(3).post("/partner/tasks/create", data={
        "title": "Default-owner task", "store_scope": "tomball",
        "category": "general", "deadline_at": _FUTURE,
    })
    assert r.status_code == 200
    assert db.get(Task, r.get_json()["task"]["id"]).owner_user_id == 3


def test_create_task_invalid_store_scope(app_with_users):
    app, client_for, db = app_with_users
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "x", "store_scope": "narnia",
        "category": "general", "deadline_at": _FUTURE,
    })
    assert r.status_code == 400


def test_create_task_invalid_category(app_with_users):
    app, client_for, db = app_with_users
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "x", "store_scope": "tomball",
        "category": "not_a_category", "deadline_at": _FUTURE,
    })
    assert r.status_code == 400


def test_create_task_past_deadline_rejected(app_with_users):
    app, client_for, db = app_with_users
    past = (datetime.utcnow() - timedelta(days=1)).isoformat()
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "x", "store_scope": "tomball",
        "category": "general", "deadline_at": past,
    })
    assert r.status_code == 400


def test_create_task_unparseable_deadline_rejected(app_with_users):
    app, client_for, db = app_with_users
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "x", "store_scope": "tomball",
        "category": "general", "deadline_at": "next tuesday-ish",
    })
    assert r.status_code == 400


def test_create_task_accepts_timezone_aware_deadline(app_with_users):
    """samai 1A review obs C: a timezone-AWARE ISO deadline must not
    500 (aware < naive-utcnow() raises TypeError). _parse_deadline
    normalizes aware → naive-UTC; a future aware deadline is accepted
    cleanly."""
    app, client_for, db = app_with_users
    # 3 days out, expressed in US Central (-05:00) — clearly future.
    future_aware = (
        datetime.utcnow() + timedelta(days=3)
    ).replace(microsecond=0).isoformat() + "-05:00"
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "tz-aware deadline",
        "store_scope": "tomball", "category": "general",
        "deadline_at": future_aware,
    })
    assert r.status_code == 200, r.data
    task = db.get(Task, r.get_json()["task"]["id"])
    # Stored naive (tzinfo stripped after UTC conversion).
    assert task.deadline_at.tzinfo is None


def test_create_task_past_timezone_aware_deadline_rejected(app_with_users):
    """A PAST timezone-aware deadline → clean 400, not a 500."""
    app, client_for, db = app_with_users
    past_aware = (
        datetime.utcnow() - timedelta(days=2)
    ).replace(microsecond=0).isoformat() + "+00:00"
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "x", "store_scope": "tomball",
        "category": "general", "deadline_at": past_aware,
    })
    assert r.status_code == 400


def test_create_task_missing_title_rejected(app_with_users):
    app, client_for, db = app_with_users
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "store_scope": "tomball",
        "category": "general", "deadline_at": _FUTURE,
    })
    assert r.status_code == 400


def test_create_task_requires_authenticated_user(app_with_users):
    """No keypad session → 403 (no g.current_user)."""
    app, client_for, db = app_with_users
    anon = app.test_client()
    r = anon.post("/partner/tasks/create", data={
        "title": "x", "store_scope": "tomball",
        "category": "general", "deadline_at": _FUTURE,
    })
    assert r.status_code in (403, 302)  # 403 from _require_user, or
                                        # 302 if the global gate bounces


def test_reassign_task_allowed(app_with_users):
    """gm(2) creates a task owned by cook(3), then reassigns to
    cook(4)... but cook(4) is copperfield, gm(2) is tomball → that
    would be cross-store. Use km(6)→cook(3) instead, both tomball."""
    app, client_for, db = app_with_users
    # gm(2) creates task owned by cook(3)
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "Reassignable task",
        "store_scope": "tomball", "category": "general",
        "deadline_at": _FUTURE,
    })
    task_id = r.get_json()["task"]["id"]
    # gm(2) reassigns to ... another tomball cook — seed one
    db.add(User(id=7, full_name="User 7", email="u7@x.test",
                passcode_hash="x", permission_level="cook",
                store_scope="tomball", active=True, first_login_done=True))
    db.commit()
    r2 = client_for(2).post(f"/partner/tasks/{task_id}/reassign", data={
        "new_owner_user_id": "7",
    })
    assert r2.status_code == 200, r2.data
    task = db.get(Task, task_id)
    assert task.owner_user_id == 7
    assert task.assigned_by_user_id == 2
    audit = db.query(TaskAuditLog).filter_by(
        task_id=task_id, action="reassigned").all()
    assert len(audit) == 1
    assert audit[0].details["from_owner_user_id"] == 3
    assert audit[0].details["to_owner_user_id"] == 7
    assert audit[0].details["reassigned_by_user_id"] == 2


def test_reassign_task_disallowed_leaves_task_unchanged(app_with_users):
    """cook(3) tries to reassign a task to gm(2) → 403, Task unchanged."""
    app, client_for, db = app_with_users
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "x", "store_scope": "tomball",
        "category": "general", "deadline_at": _FUTURE,
    })
    task_id = r.get_json()["task"]["id"]
    r2 = client_for(3).post(f"/partner/tasks/{task_id}/reassign", data={
        "new_owner_user_id": "2",
    })
    assert r2.status_code == 403
    task = db.get(Task, task_id)
    assert task.owner_user_id == 3  # unchanged
    # only the 'created' audit row exists, no 'reassigned'
    assert db.query(TaskAuditLog).filter_by(
        task_id=task_id, action="reassigned").count() == 0


def test_reassign_task_not_found(app_with_users):
    app, client_for, db = app_with_users
    r = client_for(2).post("/partner/tasks/999999/reassign", data={
        "new_owner_user_id": "3",
    })
    assert r.status_code == 404


def test_reassign_task_missing_new_owner(app_with_users):
    app, client_for, db = app_with_users
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "x", "store_scope": "tomball",
        "category": "general", "deadline_at": _FUTURE,
    })
    task_id = r.get_json()["task"]["id"]
    r2 = client_for(2).post(f"/partner/tasks/{task_id}/reassign", data={})
    assert r2.status_code == 400


# ============================================================
# Audit emission — §7 details JSON shape
# ============================================================

def test_created_audit_details_shape(app_with_users):
    app, client_for, db = app_with_users
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "Shape check",
        "store_scope": "tomball", "category": "vendor",
        "deadline_at": _FUTURE,
    })
    task_id = r.get_json()["task"]["id"]
    audit = db.query(TaskAuditLog).filter_by(
        task_id=task_id, action="created").one()
    assert set(audit.details.keys()) == {
        "owner_user_id", "store_scope", "category", "deadline_at", "title"}
    assert audit.details["owner_user_id"] == 3
    assert audit.details["store_scope"] == "tomball"
    assert audit.details["category"] == "vendor"
    assert audit.details["title"] == "Shape check"


def test_reassigned_audit_details_shape(app_with_users):
    app, client_for, db = app_with_users
    r = client_for(2).post("/partner/tasks/create", data={
        "owner_user_id": "3", "title": "x", "store_scope": "tomball",
        "category": "general", "deadline_at": _FUTURE,
    })
    task_id = r.get_json()["task"]["id"]
    db.add(User(id=8, full_name="User 8", email="u8@x.test",
                passcode_hash="x", permission_level="cook",
                store_scope="tomball", active=True, first_login_done=True))
    db.commit()
    client_for(2).post(f"/partner/tasks/{task_id}/reassign", data={
        "new_owner_user_id": "8"})
    audit = db.query(TaskAuditLog).filter_by(
        task_id=task_id, action="reassigned").one()
    assert set(audit.details.keys()) == {
        "from_owner_user_id", "to_owner_user_id", "reassigned_by_user_id"}
    assert audit.details["from_owner_user_id"] == 3
    assert audit.details["to_owner_user_id"] == 8
    assert audit.details["reassigned_by_user_id"] == 2
