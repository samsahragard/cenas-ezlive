"""Task-escalation engine — the every-5-minute cron scan (Block 1E).

Phase 2 / Block 1E (Sam's Phase 2 directive §1E, 2026-05-14). A
review-only sub-block built off the directive directly — no samai
pre-spec, same as 1D.

Three legs, run in one DB transaction per scan:

  Leg 1 — first-tier escalation. A Task past deadline_at, not
    completed, not yet escalated → escalate to the OWNER's immediate
    manager. Sets escalated_to_user_id + escalated_at, appends a
    TaskAuditLog 'escalated' row (tier 1). The task now surfaces on
    that manager's ribbon (1C reads escalated_to_user_id).

  Leg 2 — second-tier escalation. A Task escalated >=24h ago, still
    not completed, with exactly one prior 'escalated' audit row →
    escalate one tier further (the tier-1 MANAGER's own immediate
    manager), tier 2. The 'escalated' audit-row count is the cap: at
    two rows leg 2 stops matching, so escalation tops out at two tiers
    — directive §1E says "escalate one more tier up", singular. (Its
    example: Brittany missing a follow-up escalates to partner tier
    and stops there.)

  Leg 3 — SalesInsight auto-expiry. Delete SalesInsight rows past
    valid_until_at. Import-guarded — SalesInsight is Block 1F, not yet
    built; until 1F lands this leg is an inert no-op. See
    _leg3_expire_insights for the delete-vs-flag reasoning.

run_escalation_scan(db) does the work on a caller-owned Session and
returns an inspectable summary dict; it does NOT commit. The
token-gated POST /cron/task-escalation endpoint (driver_system.py)
owns the session lifecycle + commit — same division as
/cron/no-show-sweep → lifecycle.detect_no_shows(db).

This is a service module: it imports app.models + role_hierarchy at
module load. It is NOT import-safe-from-anywhere like
role_hierarchy.py — it is loaded with the driver_system blueprint at
app init. No Flask.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.models import Task, TaskAuditLog, User
from app.services.role_hierarchy import immediate_manager

log = logging.getLogger(__name__)

# Leg 2 fires once a first-tier escalation has gone this long unanswered.
_SECOND_TIER_DELAY = timedelta(hours=24)


def _maybe_sms(manager, task, owner) -> None:
    """Notify ``manager`` by SMS that ``task`` (owned by ``owner``)
    escalated to them — directive §1I.

    Block 1I (Twilio SMS) is not yet built. This is import-guarded and
    a silent no-op until it lands; escalation works fully without it.
    The module path / function name below are 1E's PROPOSED contract
    for 1I — flagged for samai / whoever builds 1I; if 1I picks a
    different shape, this one call site updates. 1I owns the
    phone-number presence check and the Twilio call; 1E just hands over
    the three rows.
    """
    try:
        from app.services.sms import notify_task_escalation  # type: ignore
    except ImportError:
        return  # Block 1I not built — escalation proceeds, just silent.
    try:
        notify_task_escalation(manager=manager, task=task, owner=owner)
    except Exception:  # noqa: BLE001
        # An SMS-provider failure must never roll back the escalation
        # DB write — log and move on.
        log.warning("escalation: SMS notify failed for task %s",
                    getattr(task, "id", "?"), exc_info=True)


def _escalate_one(db, task: Task, escalate_to: User, tier: int,
                  now: datetime) -> None:
    """Apply one escalation step to ``task``: re-point
    escalated_to_user_id at ``escalate_to``, stamp
    escalated_at/updated_at, append the 'escalated' TaskAuditLog row.
    Caller has resolved ``escalate_to`` (never None) and decided
    ``tier``.
    """
    task.escalated_to_user_id = escalate_to.id
    task.escalated_at = now
    task.updated_at = now
    db.add(TaskAuditLog(
        task_id=task.id,
        # System-initiated write: the 5-minute cron has no human actor,
        # but actor_user_id is NOT NULL. We use the task OWNER as the
        # actor, on three grounds (samai's 1E review — confirmed KEEP):
        #   - guaranteed-resolvable: owner_user_id is a NOT NULL RESTRICT
        #     FK and owners are archived, never hard-deleted;
        #   - it carries BOTH ids on one row (owner=actor, manager in
        #     details) — manager-as-actor would only duplicate details;
        #   - it keys the row onto the owner's task-history thread, the
        #     natural subject of the event.
        # 1A §7 is silent on the actor for system-initiated rows — this
        # is a spec-gap fill, not a violation (samai owns the §7
        # amendment). action="escalated" + details.tier identify the row
        # as system-driven; display surfaces (e.g. 3D's audit view) MUST
        # render 'escalated' rows by action+details, never naively as
        # "actor did X".
        actor_user_id=task.owner_user_id,
        action="escalated",
        details={
            "escalated_to_user_id": escalate_to.id,
            "tier": tier,
            "deadline_at": (task.deadline_at.isoformat()
                            if task.deadline_at else None),
        },
    ))


def _leg1_first_tier(db, now: datetime) -> dict:
    """Leg 1 — escalate freshly-overdue tasks to the owner's immediate
    manager. A task with no resolvable manager (owner is a partner, or
    nobody covers their store) is left untouched: escalated_at stays
    NULL so the next scan retries it once a manager exists.
    """
    overdue = (
        db.query(Task)
        .filter(
            Task.deadline_at < now,
            Task.completed_at.is_(None),
            Task.escalated_at.is_(None),
        )
        .all()
    )
    escalated = 0
    no_manager = 0
    for task in overdue:
        owner = db.get(User, task.owner_user_id)
        mgr = immediate_manager(owner, db) if owner is not None else None
        if mgr is None:
            no_manager += 1
            continue
        _escalate_one(db, task, mgr, tier=1, now=now)
        _maybe_sms(mgr, task, owner)
        escalated += 1
    return {"scanned": len(overdue), "escalated": escalated,
            "no_manager": no_manager}


def _leg2_second_tier(db, now: datetime) -> dict:
    """Leg 2 — a task escalated >=24h ago and still open escalates one
    more tier, to the tier-1 manager's own immediate manager. The
    'escalated' audit-row count caps it: exactly-1 prior row → fire
    (count becomes 2); 0 → data anomaly, skip; >=2 → already at tier 2,
    permanently capped.
    """
    cutoff = now - _SECOND_TIER_DELAY
    stale = (
        db.query(Task)
        .filter(
            Task.escalated_at.isnot(None),
            Task.escalated_at < cutoff,
            Task.completed_at.is_(None),
        )
        .all()
    )
    escalated = 0
    capped = 0
    no_manager = 0
    for task in stale:
        prior = (
            db.query(TaskAuditLog)
            .filter(
                TaskAuditLog.task_id == task.id,
                TaskAuditLog.action == "escalated",
            )
            .count()
        )
        if prior != 1:
            # 0 → escalated_at set with no audit row (shouldn't happen);
            # >=2 → already escalated twice, capped at two tiers.
            capped += 1
            continue
        current_mgr = db.get(User, task.escalated_to_user_id)
        # immediate_manager returns someone STRICTLY above current_mgr's
        # tier, so next_mgr can never resolve back to current_mgr or to
        # the (lower-tier) owner — no self/loop guard needed.
        next_mgr = (immediate_manager(current_mgr, db)
                    if current_mgr is not None else None)
        if next_mgr is None:
            no_manager += 1
            continue
        _escalate_one(db, task, next_mgr, tier=2, now=now)
        owner = db.get(User, task.owner_user_id)
        _maybe_sms(next_mgr, task, owner)
        escalated += 1
    return {"scanned": len(stale), "escalated": escalated,
            "capped": capped, "no_manager": no_manager}


def _leg3_expire_insights(db, now: datetime) -> dict:
    """Leg 3 — delete SalesInsight rows past their valid_until_at.

    Import-guarded: SalesInsight is Block 1F (not yet built). Until 1F
    lands this returns {"expired": 0, "available": False} and touches
    nothing.

    Why delete, not flag: 1F spec §9 — "1E's every-5m cron scans
    SalesInsight WHERE valid_until_at < now and clears them" — and the
    1F spec §2 model has NO status / resolved / expired column to flip.
    With no flag column, "clear" is a row delete. That is also the
    right semantics: a SalesInsight is ephemeral operational
    intelligence ("95F and humid today"), not an audit record — once
    past its validity window it has zero historical value, unlike a
    TaskAuditLog row. The 1C ribbon query already filters
    valid_until_at >= now, so expired rows are invisible regardless;
    this leg is housekeeping that keeps the table from growing
    unbounded. [FLAGGED for samai's 1E review — delete-vs-flag is a
    judgment call; confirm against 1F's eventual model.]
    """
    try:
        from app.models import SalesInsight  # type: ignore
    except ImportError:
        return {"expired": 0, "available": False}
    expired = (
        db.query(SalesInsight)
        .filter(SalesInsight.valid_until_at < now)
        .delete(synchronize_session=False)
    )
    return {"expired": int(expired or 0), "available": True}


def run_escalation_scan(db) -> dict:
    """Run all three escalation legs against a caller-owned Session and
    return an inspectable summary. Does NOT commit — the cron endpoint
    owns commit/rollback (same split as lifecycle.detect_no_shows).

    One logical scan, one consistent ``now`` threaded through every
    leg. Every leg is idempotent, so a crash mid-scan + rollback +
    retry on the next 5-minute tick is safe: leg 1 sets escalated_at
    (the row drops out of leg 1's filter), leg 2's audit-count guard
    caps re-firing, leg 3's delete is naturally idempotent.
    """
    now = datetime.utcnow()
    return {
        "scanned_at": now.isoformat(),
        "first_tier": _leg1_first_tier(db, now),
        "second_tier": _leg2_second_tier(db, now),
        "insight_expiry": _leg3_expire_insights(db, now),
    }
