"""Anomaly-detection rule engine.

Phase 1 / Block 1 (2026-05-13). Companion to
app/templates/docs/anomaly_service_spec.html and
app/templates/docs/anomaly_rules.html.

Public surface:
  - SignalDraft + RuleSpec dataclasses
  - @anomaly_rule decorator (registers a function into REGISTRY)
  - REGISTRY dict (rule_name -> RuleSpec)
  - run_bucket(bucket) — the engine entrypoint called by /cron/anomaly-eval

Two seed rules ship inside this module:
  - orders.no_driver_30min_before
  - orders.late_delivery

The remaining ~28 rules from anomaly_rules will land in Phase 1 / Block 5
either appended here or split into per-domain files importing
@anomaly_rule from this module.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Order, RuleOverride, Signal

logger = logging.getLogger(__name__)


# ---- public dataclasses ----

@dataclass
class SignalDraft:
    """What a rule function returns per fired condition. The engine
    handles dedup + write + side effects."""
    rule_name: str
    severity: str                # "info" | "warn" | "alert"
    store_id: str | None
    subject_id: str | None
    subject_label: str
    payload: dict
    action_text: str
    surfaces: list[str]
    audience_roles: list[str]


@dataclass
class RuleSpec:
    name: str
    bucket: str                  # "every_5m" | "every_15m" | "hourly" | "daily" | "weekly" | "on_write"
    severity_default: str
    surfaces: list[str]
    audience_roles: list[str]
    action_text: str
    fn: Callable[[Session], list[SignalDraft]]
    llm_route: str = "sql"       # "sql" | "haiku" | "opus"


# ---- registry ----

REGISTRY: dict[str, RuleSpec] = {}


def anomaly_rule(*, name: str, bucket: str, severity: str,
                 surfaces: list[str], audience_roles: list[str],
                 action_text: str, llm_route: str = "sql"):
    """Decorator: registers a rule function under REGISTRY[name]."""
    if severity not in ("info", "warn", "alert"):
        raise ValueError(f"bad severity {severity!r} for rule {name}")
    if bucket not in ("every_5m", "every_15m", "hourly",
                      "daily", "weekly", "on_write"):
        raise ValueError(f"bad bucket {bucket!r} for rule {name}")

    def deco(fn):
        REGISTRY[name] = RuleSpec(
            name=name, bucket=bucket, severity_default=severity,
            surfaces=list(surfaces), audience_roles=list(audience_roles),
            action_text=action_text, fn=fn, llm_route=llm_route,
        )
        return fn
    return deco


# Rules whose triggering condition can REVERSE on a subsequent run —
# the engine auto-resolves an open Signal whose subject no longer
# appears in the new fire-set. Non-reversible rules stay open until a
# user acknowledges them.
REVERSIBLE_RULES: set[str] = {
    "orders.no_driver_30min_before",
    "orders.late_delivery",
    "kds.station_red",
    "kds.expedite_late",
    "equipment.down",
    "attendance.no_clock_in",
}


# ---- engine internals ----

def _resolve_severity(db: Session, draft: SignalDraft) -> str:
    """If a partner has set a severity_override for (rule_name, store_id),
    use it. Otherwise use the draft's severity. Store-scoped overrides
    win over global ones."""
    overrides = (
        db.query(RuleOverride)
        .filter(RuleOverride.rule_name == draft.rule_name)
        .filter(RuleOverride.severity_override.isnot(None))
        .all()
    )
    by_store = {o.store_id: o.severity_override for o in overrides}
    if draft.store_id in by_store:
        return by_store[draft.store_id]
    if None in by_store:
        return by_store[None]
    return draft.severity


def _upsert_signal(db: Session, draft: SignalDraft) -> tuple[bool, Signal]:
    """Insert a new Signal row, OR update the open one with the same
    (rule_name, subject_id, store_id) key. Returns (was_new, row).

    'Open' means resolved_at IS NULL AND acknowledged_at IS NULL — an
    acknowledged signal is considered closed; subsequent fires create a
    fresh row so the next ack tracks the new event.
    """
    existing = (
        db.query(Signal)
        .filter(Signal.rule_name == draft.rule_name)
        .filter(Signal.subject_id == draft.subject_id)
        .filter(Signal.store_id == draft.store_id)
        .filter(Signal.resolved_at.is_(None))
        .filter(Signal.acknowledged_at.is_(None))
        .first()
    )
    severity = _resolve_severity(db, draft)
    if existing is not None:
        existing.trigger_at = datetime.utcnow()
        existing.payload = {**(existing.payload or {}), **draft.payload}
        existing.severity = severity
        existing.subject_label = draft.subject_label
        existing.action_text = draft.action_text
        existing.surfaces = draft.surfaces
        existing.audience_roles = draft.audience_roles
        return False, existing
    row = Signal(
        rule_name=draft.rule_name,
        severity=severity,
        store_id=draft.store_id,
        subject_id=draft.subject_id,
        subject_label=draft.subject_label,
        trigger_at=datetime.utcnow(),
        payload=draft.payload,
        action_text=draft.action_text,
        surfaces=draft.surfaces,
        audience_roles=draft.audience_roles,
    )
    db.add(row)
    return True, row


def _auto_clear(db: Session, rule_name: str,
                seen_keys: set[tuple]) -> int:
    """For rules in REVERSIBLE_RULES, mark resolved_at on any open
    Signal whose (rule_name, subject_id, store_id) didn't show up in
    this run. Returns the number of rows cleared.
    """
    if rule_name not in REVERSIBLE_RULES:
        return 0
    open_rows = (
        db.query(Signal)
        .filter(Signal.rule_name == rule_name)
        .filter(Signal.resolved_at.is_(None))
        .filter(Signal.acknowledged_at.is_(None))
        .all()
    )
    cleared = 0
    now = datetime.utcnow()
    for r in open_rows:
        key = (r.rule_name, r.subject_id, r.store_id)
        if key not in seen_keys:
            r.resolved_at = now
            cleared += 1
    return cleared


# ---- engine entrypoints ----

def _maybe_telegram_alert(signal: Signal) -> None:
    """Phase 1 / Block 4: fire Telegram on the initial creation of an
    alert-severity Signal. NEVER fires on:
      - severity != 'alert' (warn / info / WinSignal stay in UI)
      - updates to an existing open row (avoids re-pinging while the
        condition persists — _upsert_signal returns was_new=False)
      - if anything inside the send path raises (alerter is best-effort,
        must not bring down the engine)

    Reuses _tg_send from ezcater_webhook (existing Telegram bot creds
    via TELEGRAM_BOT_TOKEN env var; recipient is SAM_TG_CHAT_ID for
    now — both Sam and Masood want all-stores alerts at this tier).

    Phase 2 layer: per-recipient routing (GMs get their own store's
    alerts, not the cross-store firehose). Hold for permission system
    landing in ck Block 4.
    """
    if signal.severity != "alert":
        return
    try:
        from app.web.ezcater_webhook import _tg_send
        text = (
            f"🚨 ALERT · {signal.rule_name}\n"
            f"{signal.subject_label}\n"
            f"{signal.action_text}\n"
            f"Ack: https://app.cenaskitchen.com/partner/anomalies"
        )
        _tg_send(text)
    except Exception:
        logger.exception("anomaly telegram alert failed (non-fatal)")


def run_rule(db: Session, spec: RuleSpec) -> dict:
    """Run a single rule. Returns counts. Crashes are caught + logged."""
    fired = saved = updated = resolved = alerted = 0
    seen_keys: set[tuple] = set()
    try:
        drafts = spec.fn(db) or []
    except Exception:
        logger.exception("anomaly rule %s crashed (non-fatal)", spec.name)
        return {"fired": 0, "saved": 0, "updated": 0, "resolved": 0,
                "alerted": 0, "error": True}
    for d in drafts:
        fired += 1
        key = (d.rule_name, d.subject_id, d.store_id)
        seen_keys.add(key)
        was_new, row = _upsert_signal(db, d)
        if was_new:
            saved += 1
            if row.severity == "alert":
                _maybe_telegram_alert(row)
                alerted += 1
        else:
            updated += 1
    resolved = _auto_clear(db, spec.name, seen_keys)
    return {"fired": fired, "saved": saved,
            "updated": updated, "resolved": resolved,
            "alerted": alerted, "error": False}


def run_bucket(bucket: str) -> dict:
    """Run every rule registered to this bucket. One tx for the whole
    bucket — a single rule crash doesn't bring down the rest, but a DB
    write failure rolls back everything in this bucket pass.
    """
    db = SessionLocal()
    if db is None:
        logger.warning("anomaly run_bucket: SessionLocal is None — skipping")
        return {"bucket": bucket, "fired": 0, "saved": 0, "updated": 0,
                "resolved": 0, "errors": 0, "rules_run": 0}

    totals = {"fired": 0, "saved": 0, "updated": 0, "resolved": 0,
              "alerted": 0, "errors": 0, "rules_run": 0}
    try:
        for spec in list(REGISTRY.values()):
            if spec.bucket != bucket:
                continue
            stats = run_rule(db, spec)
            totals["rules_run"] += 1
            totals["fired"] += stats["fired"]
            totals["saved"] += stats["saved"]
            totals["updated"] += stats["updated"]
            totals["resolved"] += stats["resolved"]
            totals["alerted"] += stats.get("alerted", 0)
            if stats.get("error"):
                totals["errors"] += 1
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("anomaly run_bucket %s tx failed", bucket)
        raise
    finally:
        db.close()
    return {"bucket": bucket, **totals}


# ---- seed rules (Phase 1 / Block 1) ----

def _origin_to_store_slug(origin_store_id: str | None) -> str | None:
    """Map orders.origin_store_id (store_1..store_4) to the slug used
    by sidebar context (uno / dos). store_1+store_3 => uno, store_2+store_4 => dos.
    Returns None if origin is unknown.
    """
    return {
        "store_1": "uno", "store_3": "uno",
        "store_2": "dos", "store_4": "dos",
    }.get(origin_store_id or "")


@anomaly_rule(
    name="orders.no_driver_30min_before",
    bucket="every_5m",
    severity="alert",
    surfaces=["orders.by_store", "partner.anomalies", "home"],
    audience_roles=["partner", "gm", "km"],
    action_text="Assign a driver from /orders/view/<external_order_id> immediately.",
)
def no_driver_30min_before(db: Session) -> list[SignalDraft]:
    """An order whose delivery_window_start (or deliver_at fallback) is
    within 30 minutes and no driver is assigned yet."""
    now = datetime.utcnow()
    cutoff = now + timedelta(minutes=30)
    rows = (
        db.query(Order)
        .filter(Order.status.in_(("available", "requested", "new")))
        .filter(Order.assigned_driver_id.is_(None))
        .filter(Order.delivery_window_start.isnot(None))
        .filter(Order.delivery_window_start <= cutoff)
        .filter(Order.delivery_window_start >= now - timedelta(hours=2))
        .all()
    )
    out: list[SignalDraft] = []
    for o in rows:
        minutes_until = max(0,
            int((o.delivery_window_start - now).total_seconds() // 60))
        out.append(SignalDraft(
            rule_name="orders.no_driver_30min_before",
            severity="alert",
            store_id=_origin_to_store_slug(o.origin_store_id),
            subject_id=o.external_order_id or str(o.id),
            subject_label=f"Order {o.external_order_id or o.id}",
            payload={
                "minutes_until": minutes_until,
                "deliver_at": o.deliver_at,
                "delivery_address": o.delivery_address,
            },
            action_text=("Assign a driver from /orders/view/"
                         f"{o.external_order_id or o.id} immediately."),
            surfaces=["orders.by_store", "partner.anomalies", "home"],
            audience_roles=["partner", "gm", "km"],
        ))
    return out


@anomaly_rule(
    name="orders.late_delivery",
    bucket="every_5m",
    severity="warn",
    surfaces=["orders.by_store", "partner.anomalies"],
    audience_roles=["partner", "gm", "km"],
    action_text="Driver is past delivery window — call the driver or notify the customer.",
)
def late_delivery(db: Session) -> list[SignalDraft]:
    """An approved order whose delivery_window_end is in the past but
    Order.status hasn't moved to 'delivered' yet. Reversible: clears
    once the delivery is marked or cancelled.
    """
    now = datetime.utcnow()
    rows = (
        db.query(Order)
        .filter(Order.status.in_(("approved", "picked_up", "en_route")))
        .filter(Order.delivery_window_end.isnot(None))
        .filter(Order.delivery_window_end < now)
        .all()
    )
    out: list[SignalDraft] = []
    for o in rows:
        minutes_late = int((now - o.delivery_window_end).total_seconds() // 60)
        out.append(SignalDraft(
            rule_name="orders.late_delivery",
            severity="warn",
            store_id=_origin_to_store_slug(o.origin_store_id),
            subject_id=o.external_order_id or str(o.id),
            subject_label=f"Order {o.external_order_id or o.id}",
            payload={
                "minutes_late": minutes_late,
                "deliver_at": o.deliver_at,
                "delivery_address": o.delivery_address,
                "status": o.status,
                "assigned_driver_id": o.assigned_driver_id,
            },
            action_text=(f"Driver {minutes_late} min past delivery window — "
                         "call the driver or notify the customer."),
            surfaces=["orders.by_store", "partner.anomalies"],
            audience_roles=["partner", "gm", "km"],
        ))
    return out
