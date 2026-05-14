"""Anomaly cards + acknowledgment endpoint + rules admin.

Phase 1 / Block 3 + Block 7 (ck, 2026-05-13).

Three surfaces this module provides:

  1. A Jinja global `anomaly_signals_for(page_slug)` that returns the
     unresolved Signal rows for that page, filtered by the current
     user's role + current store. Templates call it directly from
     the shared partial at app/templates/partials/_anomaly_cards.html.

  2. POST /partner/anomalies/<signal_id>/ack — writes a SignalAck row
     for after-the-fact audit, stamps Signal.acknowledged_by +
     acknowledged_at, returns JSON. Once acknowledged, the card stops
     rendering for that role until the rule re-fires for a new subject.

  3. GET  /partner/anomalies/rules — Phase 1 Block 7 admin UI. Lists
     every rule registered in anomaly_engine.REGISTRY with per-rule
     fire counts (total + last-7d), last-7d ack rate, the default
     severity, and the active override if one exists. Partner edits
     severity + threshold per rule inline.

     POST /partner/anomalies/rules/<rule_name> — saves the override
     row in rule_overrides (creates one if missing). Engine consults
     these on next rule run; defaults stay in code.

Surface mounting (per anomaly_rules.html §5): a dashboard template
opts into the cards by setting `anomaly_page_slug` before its content
block. base_dashboard.html includes the partial above {% block content %};
templates without an anomaly_page_slug get nothing (silent no-op).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from flask import (
    Blueprint, jsonify, render_template, request, session, g,
    redirect, url_for, abort,
)
from sqlalchemy import desc, func

from app.db import SessionLocal
from app.models import Signal, SignalAck, RuleOverride, User
# Phase 0 Block 4 follow-up (ck 2026-05-13): tag-based gates for the
# anomaly admin pages + a coarse entry-tag for the ack endpoint. The
# per-signal audience-eligibility check inside acknowledge_signal stays
# — that's audience-correctness, distinct from the role-can-ack-at-all
# entry gate the decorator provides.
from app.services.permissions import requires_permission

anomaly = Blueprint("anomaly", __name__)


# Map User.permission_level → the set of audience role strings the
# user counts against (partner sees partner + corporate signals,
# corporate sees corporate, GM sees manager + corporate operations,
# etc). Empty audience_roles[] on a signal = visible to everyone.
_ROLE_SETS = {
    "partner":          {"partner", "corporate", "gm", "manager", "expo"},
    "corporate":        {"corporate", "gm", "manager", "expo"},
    "gm":               {"gm", "manager", "expo"},
    "manager":          {"manager", "expo"},
    "expo":             {"expo"},
    "corporate-driver": {"corporate-driver", "driver"},
}


def _user_role_set() -> set[str]:
    u = getattr(g, "current_user", None)
    if u is None:
        # Anonymous / partner-password-only sessions can still see signals
        # with empty audience_roles[] but won't match anything role-specific.
        return set()
    return _ROLE_SETS.get(u.permission_level, {u.permission_level})


def _current_store_id() -> str | None:
    """Return the slug for the in-scope store, or None for cross-store views.

    Signals with store_id IS NULL show everywhere; signals with a
    specific store_id only show when g.current_store matches or is
    'partner' / 'corporate' (those see everything)."""
    slug = getattr(g, "current_store", None)
    if slug in (None, "partner", "corporate"):
        return None  # → show all-store signals
    # store_routes maps 'dos' → 'tomball' and 'uno' → 'copperfield'.
    # Rules use the location strings, not the slugs, so translate.
    return getattr(g, "current_location", None) or slug


def anomaly_signals_for(page_slug: str, limit: int = 5) -> list[Signal]:
    """Jinja-exposed callable. Returns up to `limit` unresolved Signals
    that target the given page slug AND match the current user's role
    set AND match the current store scope. SQLite-compatible (we
    filter the JSON-array overlap in Python since SQLite has no
    native array containment operator)."""
    if not page_slug:
        return []
    role_set = _user_role_set()
    store_filter = _current_store_id()
    db = SessionLocal()
    try:
        # Pull all candidates (small table: signals at our scale never
        # crosses 1000s of unresolved rows). Filter in Python.
        q = (db.query(Signal)
               .filter(Signal.acknowledged_at.is_(None))
               .filter(Signal.resolved_at.is_(None))
               .order_by(desc(Signal.severity), desc(Signal.trigger_at)))
        out: list[Signal] = []
        for s in q.all():
            # Surface match — every Signal carries its surfaces[] list.
            if page_slug not in (s.surfaces or []):
                continue
            # Audience match — empty list = everyone; otherwise overlap.
            aud = s.audience_roles or []
            if aud and not (role_set & set(aud)):
                continue
            # Store match — NULL = all stores; otherwise must match the
            # in-scope store. Partner / corporate views see everything.
            if s.store_id and store_filter and s.store_id != store_filter:
                continue
            out.append(s)
            if len(out) >= limit:
                break
        return out
    finally:
        db.close()


@anomaly.route("/partner/anomalies/<int:signal_id>/ack", methods=["POST"])
@requires_permission("kds.view_alerts")
def acknowledge_signal(signal_id: int):
    """Acknowledge a Signal. Writes one SignalAck row for audit, stamps
    Signal.acknowledged_by + Signal.acknowledged_at. Idempotent — if the
    signal is already acked we still write a new SignalAck row (so a
    repeated click captures the second interaction) but the signal's
    acknowledged_at sticks at the first ack.

    Authorization: the caller must be in the signal's audience (role
    overlap with audience_roles AND store scope matches store_id).
    Partner-tier sessions pass the audience check trivially (their
    role set is a superset of all in-app roles). This closes the
    authorization gap samai caught in her Block 3 review — without
    the check, any keypad-authed user could ack any signal they
    happen to know the id of."""
    # Gate on partner-tier or a signed-in keypad user. Driver portal has
    # its own surface and shouldn't ack via this URL.
    u = getattr(g, "current_user", None)
    if not u and not session.get("partner_auth_ok"):
        return jsonify({"ok": False, "error": "not signed in"}), 401
    note = (request.get_json(silent=True) or {}).get("note", "") or ""
    note = note.strip()[:400] or None
    db = SessionLocal()
    try:
        sig = db.get(Signal, signal_id)
        if sig is None:
            return jsonify({"ok": False, "error": "signal not found"}), 404

        # Audience-eligibility check BEFORE mutation. Partner-Tier-2-only
        # sessions (no User in g.current_user) are treated as partner +
        # see everything; otherwise we apply the same role-set / store-
        # scope filter the read-side uses to decide whether to render
        # this signal as a card. Mismatched user → 403.
        if u is not None:
            user_roles = _user_role_set()
            aud = sig.audience_roles or []
            if aud and not (user_roles & set(aud)):
                return jsonify({
                    "ok": False,
                    "error": "not in this signal's audience",
                }), 403
            # Store scope: NULL store_id = global signal, ack OK from any
            # store context. Otherwise the user's current store has to
            # match — partner / corporate views are cross-store so the
            # _current_store_id helper returns None for them and we let
            # those through.
            scope = _current_store_id()
            if sig.store_id and scope and sig.store_id != scope:
                return jsonify({
                    "ok": False,
                    "error": "signal is scoped to a different store",
                }), 403

        now = datetime.utcnow()
        actor_id = u.id if u else None
        if actor_id is None:
            # Partner-password-only session — record under the
            # partner-bootstrap User if one exists, otherwise skip.
            seed = db.query(User).filter(User.permission_level == "partner").first()
            actor_id = seed.id if seed else None
        if actor_id is None:
            return jsonify({"ok": False, "error": "no acker identity"}), 401
        # Stamp the first ack only — later clicks log the second person.
        if sig.acknowledged_at is None:
            sig.acknowledged_by = actor_id
            sig.acknowledged_at = now
        db.add(SignalAck(
            signal_id=sig.id,
            user_id=actor_id,
            acked_at=now,
            note=note,
        ))
        db.commit()
        return jsonify({
            "ok": True,
            "signal_id": sig.id,
            "acknowledged_at": sig.acknowledged_at.isoformat() if sig.acknowledged_at else None,
        })
    finally:
        db.close()


# ============================================================
# Block 7 — Rules admin (/partner/anomalies/rules)
# ============================================================

def _enforce_partner():
    """Same shape as legal_routes / developer_chat — partner_auth_ok
    Tier-2 flag from the keypad-side login."""
    if not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login", next=request.path))
    return None


@anomaly.route("/partner/anomalies/rules", methods=["GET"])
@requires_permission("anomaly.admin")
def rules_admin():
    """List every registered rule with stats + the active override.

    Stats per rule (last 7d window):
      - total_fires: COUNT(Signal) WHERE rule_name = X
      - fires_7d:    COUNT(Signal) WHERE rule_name = X AND trigger_at >= now-7d
      - acks_7d:     COUNT(Signal) WHERE … AND acknowledged_at IS NOT NULL
      - ack_rate_7d: acks_7d / fires_7d (None when fires_7d == 0)
    """
    gate = _enforce_partner()
    if gate is not None:
        return gate
    from app.services.anomaly_engine import REGISTRY
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        week_ago = now - timedelta(days=7)

        # Bulk-fetch stats so we don't hit N+1 against signals. Two passes
        # (all-time + last-7d) so a rule with no fires still surfaces.
        totals = dict(db.query(Signal.rule_name, func.count(Signal.id))
                        .group_by(Signal.rule_name).all())
        fires_7d = dict(db.query(Signal.rule_name, func.count(Signal.id))
                          .filter(Signal.trigger_at >= week_ago)
                          .group_by(Signal.rule_name).all())
        acks_7d = dict(db.query(Signal.rule_name, func.count(Signal.id))
                         .filter(Signal.trigger_at >= week_ago)
                         .filter(Signal.acknowledged_at.isnot(None))
                         .group_by(Signal.rule_name).all())
        overrides = {(o.rule_name, o.store_id): o
                     for o in db.query(RuleOverride).all()}

        rows = []
        for name, spec in sorted(REGISTRY.items()):
            # Global override (store_id IS NULL) is the one this admin
            # surface manages. Per-store overrides are TBD; flag if any.
            ovr = overrides.get((name, None))
            store_specific = [o for k, o in overrides.items()
                              if k[0] == name and k[1] is not None]
            total = totals.get(name, 0)
            seven = fires_7d.get(name, 0)
            ack = acks_7d.get(name, 0)
            ack_rate = (ack / seven) if seven else None
            rows.append({
                "name": name,
                "spec": spec,
                "override": ovr,
                "store_overrides_count": len(store_specific),
                "total_fires": total,
                "fires_7d": seven,
                "acks_7d": ack,
                "ack_rate_7d": ack_rate,
            })

        return render_template(
            "anomaly_rules_admin.html",
            rules=rows,
            active="anomaly_rules",
            success=request.args.get("success"),
            error=request.args.get("error"),
        )
    finally:
        db.close()


@anomaly.route("/partner/anomalies/rules/<rule_name>", methods=["POST"])
@requires_permission("anomaly.admin")
def rules_admin_save(rule_name: str):
    """Save a global override (store_id NULL) for one rule. Inserts a
    new RuleOverride row if none exists, otherwise updates. Engine
    reads these before each rule run.

    Form fields:
      severity_override  empty | info | warn | alert
      threshold          free-form JSON object (validated)
    """
    gate = _enforce_partner()
    if gate is not None:
        return gate
    from app.services.anomaly_engine import REGISTRY
    if rule_name not in REGISTRY:
        return redirect(url_for("anomaly.rules_admin",
                                error=f"Unknown rule '{rule_name}'."))
    sev = (request.form.get("severity_override") or "").strip().lower()
    if sev and sev not in ("info", "warn", "alert"):
        return redirect(url_for("anomaly.rules_admin",
                                error="Severity must be info, warn, alert, or empty."))
    threshold_raw = (request.form.get("threshold") or "").strip()
    threshold: dict = {}
    if threshold_raw:
        try:
            parsed = json.loads(threshold_raw)
            if not isinstance(parsed, dict):
                raise ValueError("must be a JSON object")
            threshold = parsed
        except (json.JSONDecodeError, ValueError) as e:
            return redirect(url_for("anomaly.rules_admin",
                                    error=f"Threshold must be a JSON object: {e}"))
    u = getattr(g, "current_user", None)
    actor_id = u.id if u else None
    if actor_id is None:
        # Fall back to the partner-seed User if we only have Tier-2.
        db_tmp = SessionLocal()
        try:
            seed = db_tmp.query(User).filter(
                User.permission_level == "partner").first()
            actor_id = seed.id if seed else None
        finally:
            db_tmp.close()
    if actor_id is None:
        return redirect(url_for("anomaly.rules_admin",
                                error="No partner identity to attribute override."))

    db = SessionLocal()
    try:
        ovr = (db.query(RuleOverride)
                 .filter(RuleOverride.rule_name == rule_name)
                 .filter(RuleOverride.store_id.is_(None))
                 .first())
        cleared = False
        if not sev and not threshold:
            # Nothing to store → delete the override row if it existed.
            if ovr is not None:
                db.delete(ovr)
                cleared = True
        else:
            if ovr is None:
                ovr = RuleOverride(
                    rule_name=rule_name, store_id=None,
                    threshold={}, severity_override=None,
                    updated_by=actor_id,
                )
                db.add(ovr)
            ovr.severity_override = sev or None
            ovr.threshold = threshold
            ovr.updated_by = actor_id
        db.commit()
        msg = (f"Override cleared for {rule_name}."
               if cleared else f"Override saved for {rule_name}.")
        return redirect(url_for("anomaly.rules_admin", success=msg))
    finally:
        db.close()


def install(app):
    """Register the blueprint and the Jinja global. Called from
    app.create_app() after the blueprint imports settle."""
    app.register_blueprint(anomaly)
    app.jinja_env.globals["anomaly_signals_for"] = anomaly_signals_for
