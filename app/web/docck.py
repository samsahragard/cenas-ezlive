"""docck v1 — Multi-agent reliability monitor Blueprint.

Per Sam #1191 amendment to the 5-layer cena reliability spec.

Endpoints:
  POST /docck/heartbeat/<agent_id>     — agents POST their heartbeat here
  GET  /docck/status                   — full agent map
  GET  /docck/status/<agent_id>        — single agent
  POST /docck/tick                     — Render Cron triggers this every 60s
  POST /docck/admin/silence            — global silence
  POST /docck/admin/<agent_id>/silence — agent-scoped silence
  POST /docck/admin/<agent_id>/force_recovery — reset breaker + start sequence
  POST /docck/admin/<agent_id>/cancel_sequence — cancel active sequence

Auth:
  /heartbeat/<agent_id> — Bearer token, verified against agents.heartbeat_token_hash
  /status               — public (read-only, no secrets exposed)
  /tick                 — Bearer token (DOCCK_TICK_TOKEN env var)
  /admin/*              — Bearer token (DOCCK_ADMIN_TOKEN env var)

DROP THIS INTO: app/web/docck.py (new file)
REGISTER IN:    app/__init__.py — `from app.web.docck import bp as docck_bp; app.register_blueprint(docck_bp)`
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Any

from flask import Blueprint, jsonify, request
from sqlalchemy import select, func, desc
from werkzeug.security import check_password_hash

from app.db import SessionLocal
from app.models import (
    DocckAgent,
    DocckHeartbeat,
    DocckRestartSequence,
    DocckRestartStep,
    DocckAlertSent,
    DocckCircuitBreaker,
)

bp = Blueprint("docck", __name__, url_prefix="/docck")
log = logging.getLogger(__name__)


# ============================================================
# Rate limiter — in-memory, per-agent, sliding window
# ============================================================
# Heartbeat: 10 per minute per agent_id. Anything over → 429.
_RATE_BUCKETS: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=20))


def _rate_limit_heartbeat(agent_id: str) -> bool:
    """Returns True if request is allowed, False if over limit."""
    now = time.monotonic()
    bucket = _RATE_BUCKETS[agent_id]
    # purge entries older than 60s
    while bucket and now - bucket[0] > 60.0:
        bucket.popleft()
    if len(bucket) >= 10:
        return False
    bucket.append(now)
    return True


# ============================================================
# /heartbeat/<agent_id>
# ============================================================
@bp.route("/heartbeat/<agent_id>", methods=["POST"])
def heartbeat(agent_id: str):
    """Agents POST every ~30s. Body shape per Contract 1 (samai #1208 frozen v1)."""
    # Rate limit FIRST (cheap, before DB hit)
    if not _rate_limit_heartbeat(agent_id):
        return jsonify({"error": "rate_limited"}), 429

    # Auth
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return jsonify({"error": "unauthorized"}), 401
    token = auth[7:].strip()

    sess = SessionLocal()
    try:
        agent = sess.get(DocckAgent, agent_id)
        if agent is None or not agent.enabled:
            return jsonify({"error": "agent_not_found_or_disabled"}), 404

        if not check_password_hash(agent.heartbeat_token_hash, token):
            return jsonify({"error": "unauthorized"}), 401

        body = request.get_json(silent=True) or {}

        # Parse + validate (loose — accept missing optional fields, never crash)
        def _iso(v: Any) -> datetime | None:
            if not v:
                return None
            try:
                s = str(v).rstrip("Z").rstrip("z")
                return datetime.fromisoformat(s)
            except Exception:
                return None

        hb = DocckHeartbeat(
            agent_id=agent_id,
            received_at=datetime.utcnow(),
            agent_timestamp=_iso(body.get("timestamp")),
            agent_state=str(body.get("agent_state") or "")[:32] or None,
            agent_version=str(body.get("agent_version") or "")[:64] or None,
            model_active=str(body.get("model_active") or "")[:128] or None,
            last_anthropic_api_call_at=_iso(body.get("last_anthropic_api_call_at")),
            memory_mb=int(body.get("memory_mb") or 0) or None,
            cpu_pct=float(body.get("cpu_pct") or 0.0) or None,
            uptime_seconds=int(body.get("uptime_seconds") or 0) or None,
            in_flight_requests=int(body.get("in_flight_requests") or 0),
            extras=body.get("extras") if isinstance(body.get("extras"), dict) else None,
        )
        sess.add(hb)
        sess.commit()

        return jsonify({
            "received": True,
            "agent_id": agent_id,
            "server_time": datetime.utcnow().isoformat() + "Z",
        })
    except Exception:
        log.exception("docck.heartbeat failed for agent %s", agent_id)
        sess.rollback()
        return jsonify({"error": "internal"}), 500
    finally:
        sess.close()


# ============================================================
# /status + /status/<agent_id>
# ============================================================
def _agent_status_dict(sess, agent: DocckAgent) -> dict:
    last_hb = sess.execute(
        select(DocckHeartbeat).where(DocckHeartbeat.agent_id == agent.id)
        .order_by(desc(DocckHeartbeat.received_at)).limit(1)
    ).scalar_one_or_none()

    seq = sess.execute(
        select(DocckRestartSequence).where(
            DocckRestartSequence.agent_id == agent.id,
            DocckRestartSequence.ended_at.is_(None),
        ).order_by(desc(DocckRestartSequence.started_at)).limit(1)
    ).scalar_one_or_none()

    breaker = sess.get(DocckCircuitBreaker, agent.id)

    now = datetime.utcnow()
    seconds_since = None
    if last_hb:
        seconds_since = int((now - last_hb.received_at).total_seconds())

    # State derivation:
    #   alive   = heartbeat within 60s
    #   slow    = heartbeat 60-180s ago
    #   missing = no heartbeat for >180s
    #   never   = no heartbeat ever
    if last_hb is None:
        state = "never"
    elif seconds_since is None or seconds_since > 180:
        state = "missing"
    elif seconds_since > 60:
        state = "slow"
    else:
        state = "alive"

    return {
        "id": agent.id,
        "display_name": agent.display_name,
        "machine_label": agent.machine_label,
        "enabled": agent.enabled,
        "state": state,
        "last_heartbeat_at": last_hb.received_at.isoformat() + "Z" if last_hb else None,
        "seconds_since_heartbeat": seconds_since,
        "last_agent_state": last_hb.agent_state if last_hb else None,
        "last_anthropic_api_call_at": last_hb.last_anthropic_api_call_at.isoformat() + "Z" if (last_hb and last_hb.last_anthropic_api_call_at) else None,
        "active_restart_sequence_id": seq.id if seq else None,
        "circuit_breaker_open": (breaker is not None and (breaker.manually_tripped or breaker.failed_sequence_count >= 3)),
    }


@bp.route("/status", methods=["GET"])
def status_all():
    sess = SessionLocal()
    try:
        agents = sess.execute(select(DocckAgent)).scalars().all()
        return jsonify({
            "agents": {a.id: _agent_status_dict(sess, a) for a in agents},
            "docck": {
                "version": "1.0.0",
                "server_time": datetime.utcnow().isoformat() + "Z",
            },
        })
    finally:
        sess.close()


@bp.route("/status/<agent_id>", methods=["GET"])
def status_one(agent_id: str):
    sess = SessionLocal()
    try:
        agent = sess.get(DocckAgent, agent_id)
        if agent is None:
            return jsonify({"error": "not_found"}), 404
        return jsonify(_agent_status_dict(sess, agent))
    finally:
        sess.close()


# ============================================================
# /tick — fired by Render Cron every 60s
# ============================================================
def _check_tick_auth() -> bool:
    expected = (os.getenv("DOCCK_TICK_TOKEN") or "").strip()
    if not expected:
        # If no token configured, only allow same-host (localhost) — safe default
        return request.remote_addr in ("127.0.0.1", "::1")
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {expected}"


@bp.route("/tick", methods=["POST"])
def tick():
    """Render Cron fires this every 60s. Iterate enabled agents, evaluate,
    fire restart sequences if needed. v1 ALERT-ONLY mode: posts alerts to
    dev chat, does NOT yet fire restart sequences (those land in v1.1 after
    aick + ck deploy watchdogs)."""
    if not _check_tick_auth():
        return jsonify({"error": "unauthorized"}), 401

    sess = SessionLocal()
    fired: list[str] = []
    try:
        agents = sess.execute(
            select(DocckAgent).where(DocckAgent.enabled == True)
        ).scalars().all()
        for agent in agents:
            status = _agent_status_dict(sess, agent)
            seconds = status.get("seconds_since_heartbeat")
            state = status.get("state")

            # v1 alert-only: post to dev chat on missing / slow transitions,
            # deduped by (agent_id, state) within 5min window.
            if state in ("missing", "never") or (seconds is not None and seconds > agent.alert_telegram_threshold_seconds):
                _post_alert_deduped(
                    sess=sess,
                    agent_id=agent.id,
                    severity="warn" if state == "slow" else "urgent",
                    channel="dev_chat",
                    dedupe_key=f"{agent.id}_{state}_v1",
                    body=f"[docck] {agent.display_name} {state.upper()} — {seconds}s since last heartbeat",
                    dedupe_window_seconds=300,
                )
                fired.append(agent.id)
        sess.commit()
        return jsonify({"ok": True, "agents_evaluated": len(agents), "alerts_fired": fired})
    except Exception:
        log.exception("docck.tick failed")
        sess.rollback()
        return jsonify({"error": "internal"}), 500
    finally:
        sess.close()


def _post_alert_deduped(sess, agent_id: str, severity: str, channel: str,
                         dedupe_key: str, body: str, dedupe_window_seconds: int = 300) -> bool:
    """Post an alert if not already posted within the dedupe window. Returns
    True if posted, False if suppressed. Writes the alert row regardless of
    actual delivery success — that's a recovery problem, not a dedupe one."""
    cutoff = datetime.utcnow() - timedelta(seconds=dedupe_window_seconds)
    existing = sess.execute(
        select(DocckAlertSent).where(
            DocckAlertSent.dedupe_key == dedupe_key,
            DocckAlertSent.sent_at > cutoff,
        ).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return False

    # Record + send
    row = DocckAlertSent(
        agent_id=agent_id, severity=severity, channel=channel,
        dedupe_key=dedupe_key, body=body, sent_at=datetime.utcnow(),
    )
    sess.add(row)

    # Best-effort post to dev chat (don't crash if it fails)
    try:
        _post_to_dev_chat(body)
    except Exception:
        log.exception("docck: dev_chat post failed (alert row recorded anyway)")

    return True


def _post_to_dev_chat(body: str) -> None:
    """Post a message to the LAN dev chat as 'docck' via the bridge on ck.
    Uses the bridge URL configured via DOCCK_DEV_CHAT_POST_URL env var.
    No-op if the env var is not set (dev environment)."""
    url = (os.getenv("DOCCK_DEV_CHAT_POST_URL") or "").strip()
    if not url:
        log.info("docck alert (no DOCCK_DEV_CHAT_POST_URL): %s", body)
        return
    import urllib.request, urllib.parse
    data = urllib.parse.urlencode({"author": "docck", "body": body}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


# ============================================================
# /admin/* — manual interventions
# ============================================================
def _check_admin_auth() -> bool:
    expected = (os.getenv("DOCCK_ADMIN_TOKEN") or "").strip()
    if not expected:
        return False  # closed unless explicitly enabled
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {expected}"


@bp.route("/admin/<agent_id>/silence", methods=["POST"])
def admin_silence(agent_id: str):
    if not _check_admin_auth():
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    minutes = int(body.get("minutes") or 60)
    sess = SessionLocal()
    try:
        agent = sess.get(DocckAgent, agent_id)
        if agent is None:
            return jsonify({"error": "not_found"}), 404
        breaker = sess.get(DocckCircuitBreaker, agent_id) or DocckCircuitBreaker(agent_id=agent_id)
        breaker.manually_tripped = True
        breaker.window_start = datetime.utcnow() + timedelta(minutes=minutes)
        sess.merge(breaker)
        sess.commit()
        return jsonify({"ok": True, "silenced_until": breaker.window_start.isoformat() + "Z"})
    finally:
        sess.close()


@bp.route("/admin/<agent_id>/force_recovery", methods=["POST"])
def admin_force_recovery(agent_id: str):
    """Reset the breaker. State machine (v1.1) will start a sequence next tick."""
    if not _check_admin_auth():
        return jsonify({"error": "unauthorized"}), 401
    sess = SessionLocal()
    try:
        breaker = sess.get(DocckCircuitBreaker, agent_id)
        if breaker:
            breaker.manually_tripped = False
            breaker.failed_sequence_count = 0
            sess.commit()
        return jsonify({"ok": True})
    finally:
        sess.close()
