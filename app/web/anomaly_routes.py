"""Anomaly cards + acknowledgment endpoint.

Phase 1 / Block 3 (ck, 2026-05-13).

Two surfaces this module provides:

  1. A Jinja global `anomaly_signals_for(page_slug)` that returns the
     unresolved Signal rows for that page, filtered by the current
     user's role + current store. Templates call it directly from
     the shared partial at app/templates/partials/_anomaly_cards.html.

  2. POST /partner/anomalies/<signal_id>/ack — writes a SignalAck row
     for after-the-fact audit, stamps Signal.acknowledged_by +
     acknowledged_at, returns JSON. Once acknowledged, the card stops
     rendering for that role until the rule re-fires for a new subject.

Surface mounting (per anomaly_rules.html §5): a dashboard template
opts into the cards by setting `anomaly_page_slug` before its content
block. base_dashboard.html includes the partial above {% block content %};
templates without an anomaly_page_slug get nothing (silent no-op).
"""
from __future__ import annotations

from datetime import datetime

from flask import (
    Blueprint, jsonify, request, session, g, redirect, url_for, abort,
)
from sqlalchemy import desc

from app.db import SessionLocal
from app.models import Signal, SignalAck, User

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
def acknowledge_signal(signal_id: int):
    """Acknowledge a Signal. Writes one SignalAck row for audit, stamps
    Signal.acknowledged_by + Signal.acknowledged_at. Idempotent — if the
    signal is already acked we still write a new SignalAck row (so a
    repeated click captures the second interaction) but the signal's
    acknowledged_at sticks at the first ack."""
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


def install(app):
    """Register the blueprint and the Jinja global. Called from
    app.create_app() after the blueprint imports settle."""
    app.register_blueprint(anomaly)
    app.jinja_env.globals["anomaly_signals_for"] = anomaly_signals_for
