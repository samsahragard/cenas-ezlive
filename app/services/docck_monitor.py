"""docck self-driven monitoring loop.

REPLACES the external DocckTickFirer Scheduled Task (which was an
architectural mistake — it made docck depend on aick being up, defeating
the point of docck living in a separate failure domain on Render).

Now docck ticks itself from a background thread that starts at app boot.
Multi-worker safe: the app runs under gunicorn with 2 workers, so each
worker starts a ticker thread, but a DB lease ensures exactly ONE worker
actually evaluates on each tick. If the lease-holder worker dies, another
acquires the lease within the TTL (90s) and takes over — so the monitor
survives a worker crash.

DROP INTO: app/services/docck_monitor.py (new file)
WIRE IN:   app/__init__.py — after blueprint registration:
             from app.services.docck_monitor import start_background_ticker
             start_background_ticker()
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import (
    DocckAgent,
    DocckHeartbeat,
    DocckAlertSent,
    DocckTickLease,
)

log = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 30
LEASE_TTL_SECONDS = 90
_WORKER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_LEASE_ROW_ID = 1

_started = False
_started_lock = threading.Lock()


# ============================================================
# Lease — ensures exactly one worker evaluates per tick
# ============================================================
def _acquire_or_renew_lease(sess) -> bool:
    """Atomic compare-and-set on the singleton lease row. Returns True if
    THIS worker now holds the lease. Safe across gunicorn workers because
    the UPDATE...WHERE is atomic at the DB level."""
    now = datetime.utcnow()
    new_expiry = now + timedelta(seconds=LEASE_TTL_SECONDS)

    # Ensure the singleton row exists (idempotent).
    row = sess.get(DocckTickLease, _LEASE_ROW_ID)
    if row is None:
        try:
            sess.add(DocckTickLease(id=_LEASE_ROW_ID, holder=None, expires_at=now - timedelta(seconds=1)))
            sess.commit()
        except Exception:
            sess.rollback()  # another worker created it first — fine

    # CAS: take the lease iff it's ours already OR it has expired.
    result = sess.execute(
        text(
            "UPDATE docck_tick_lease SET holder = :me, expires_at = :exp "
            "WHERE id = :rid AND (holder = :me OR holder IS NULL OR expires_at < :now)"
        ),
        {"me": _WORKER_ID, "exp": new_expiry, "rid": _LEASE_ROW_ID, "now": now},
    )
    sess.commit()
    return result.rowcount == 1


# ============================================================
# Tick evaluation — the actual monitoring logic
# ============================================================
def _agent_state(sess, agent: DocckAgent) -> tuple[str, int | None]:
    """Return (state, seconds_since_heartbeat) for an agent."""
    last_hb = sess.execute(
        select(DocckHeartbeat).where(DocckHeartbeat.agent_id == agent.id)
        .order_by(DocckHeartbeat.received_at.desc()).limit(1)
    ).scalar_one_or_none()
    if last_hb is None:
        return "never", None
    secs = int((datetime.utcnow() - last_hb.received_at).total_seconds())
    if secs > 180:
        return "missing", secs
    if secs > 60:
        return "slow", secs
    return "alive", secs


def _post_alert_deduped(sess, agent_id, severity, channel, dedupe_key, body,
                        dedupe_window_seconds=300) -> bool:
    cutoff = datetime.utcnow() - timedelta(seconds=dedupe_window_seconds)
    existing = sess.execute(
        select(DocckAlertSent).where(
            DocckAlertSent.dedupe_key == dedupe_key,
            DocckAlertSent.sent_at > cutoff,
        ).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return False
    sess.add(DocckAlertSent(
        agent_id=agent_id, severity=severity, channel=channel,
        dedupe_key=dedupe_key, body=body, sent_at=datetime.utcnow(),
    ))
    try:
        _post_to_dev_chat(body)
    except Exception:
        log.exception("docck: dev_chat post failed (alert row recorded anyway)")
    return True


def _post_to_dev_chat(body: str) -> None:
    """Insert a developer_chat row DIRECTLY. docck runs on Render with the same
    DB, so this is more robust than HTTP-POSTing to /partner/developer/chat/post
    (which is partner-auth-gated and would 302 an unauthenticated background
    thread). ck's bridge.py mirrors developer_chat rows to the LAN hub + Telegram,
    and samai's chat_tail reads the same Render table — so a direct insert shows
    up on every surface. (Sam #1257 fix: previous HTTP-to-LAN-hub path failed with
    auth/Tailscale errors; direct insert removes that dependency.)"""
    from app.models import DeveloperChatMessage
    sess = SessionLocal()
    try:
        sess.add(DeveloperChatMessage(author="docck", body=body[:4000]))
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def run_tick_evaluation() -> dict:
    """Evaluate all enabled agents once. Posts alerts on missing/never.
    v1 = ALERT-ONLY. v1.1 will add the restart-sequence executor here.
    Returns a summary dict. Safe to call from the background thread OR the
    /docck/tick endpoint (manual trigger)."""
    sess = SessionLocal()
    fired: list[str] = []
    try:
        agents = sess.execute(
            select(DocckAgent).where(DocckAgent.enabled == True)  # noqa: E712
        ).scalars().all()
        for agent in agents:
            state, secs = _agent_state(sess, agent)
            if state in ("missing", "never"):
                # Only count as "fired" if a NEW alert was actually posted
                # (i.e. not suppressed by the 5-min dedupe window). Previously
                # this appended unconditionally, which made every tick look like
                # it re-alerted even when deduped (Sam #1257 diagnostic noise).
                posted = _post_alert_deduped(
                    sess=sess, agent_id=agent.id, severity="urgent",
                    channel="dev_chat", dedupe_key=f"{agent.id}_{state}_v1",
                    body=f"[docck] {agent.display_name} {state.upper()} — "
                         f"{secs if secs is not None else 'no'} s since last heartbeat. "
                         f"(machine {agent.machine_label})",
                    dedupe_window_seconds=300,
                )
                if posted:
                    fired.append(agent.id)
        sess.commit()
        return {"ok": True, "agents_evaluated": len(agents), "alerts_fired": fired}
    except Exception:
        log.exception("docck.run_tick_evaluation failed")
        sess.rollback()
        return {"ok": False}
    finally:
        sess.close()


# ============================================================
# Background ticker thread
# ============================================================
def _ticker_loop():
    log.info("docck ticker thread started (worker %s, interval %ds, lease TTL %ds)",
             _WORKER_ID, TICK_INTERVAL_SECONDS, LEASE_TTL_SECONDS)
    while True:
        try:
            sess = SessionLocal()
            try:
                have_lease = _acquire_or_renew_lease(sess)
            finally:
                sess.close()
            if have_lease:
                summary = run_tick_evaluation()
                if summary.get("alerts_fired"):
                    log.warning("docck tick: alerts fired %s", summary["alerts_fired"])
        except Exception:
            log.exception("docck ticker loop iteration failed (continuing)")
        time.sleep(TICK_INTERVAL_SECONDS)


def start_background_ticker() -> None:
    """Start the ticker thread once per worker process. Idempotent within a
    process. The DB lease coordinates across the 2 gunicorn workers so only
    one evaluates per tick."""
    global _started
    with _started_lock:
        if _started:
            return
        _started = True
    t = threading.Thread(target=_ticker_loop, name="docck-ticker", daemon=True)
    t.start()
    log.info("docck background ticker launched in worker %s", _WORKER_ID)
