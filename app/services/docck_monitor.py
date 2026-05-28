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

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, text

from app.db import SessionLocal
from app.models import (
    DocckAgent,
    DocckHeartbeat,
    DocckAlertSent,
    DocckTickLease,
    DocckRestartSequence,
    DocckRestartStep,
    DocckCircuitBreaker,
)

log = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 30
LEASE_TTL_SECONDS = 90
_WORKER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
_LEASE_ROW_ID = 1

# ---- v1.1 auto-recovery config ----
# MASTER SAFETY FLAG. When OFF (default), the state machine runs in DRY-RUN:
# it records what it WOULD do (sequence + steps) and alerts, but fires NO
# watchdog calls and reboots nothing. Flip DOCCK_AUTORECOVER_ENABLED=1 on
# Render to arm real auto-recovery. This is the blast-radius guard (Sam #1257).
AUTORECOVER_ENABLED = (os.getenv("DOCCK_AUTORECOVER_ENABLED", "").strip().lower()
                       in ("1", "true", "yes", "on"))
# SEPARATE, stricter gate for machine reboots. Arming AUTORECOVER enables low-blast
# SERVICE restarts; rebooting a live business machine is high-blast and stays OFF
# until the state machine has a track record (Sam #1257 blast-radius posture).
# When off, a reboot_machine step is skipped + the sequence escalates to a human
# alert instead. Flip DOCCK_ALLOW_REBOOT=1 to enable auto-reboot as a last resort.
ALLOW_REBOOT = (os.getenv("DOCCK_ALLOW_REBOOT", "").strip().lower()
                in ("1", "true", "yes", "on"))
CB_THRESHOLD = 3                 # failed sequences within the window trips the breaker
CB_WINDOW_SECONDS = 3600         # 1 hour
# Active-recovery threads, keyed by agent_id, so the 30s tick never spawns a
# second sequence for an agent that's already recovering.
_active_recoveries: dict[str, threading.Thread] = {}
_active_lock = threading.Lock()

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
        recoveries: list[str] = []
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
                # v1.1: attempt auto-recovery. 'missing' only (not 'never' — a
                # never-seen agent has no daemon to restart yet). Gated inside
                # by circuit breaker + active-sequence + AUTORECOVER flag.
                if state == "missing":
                    status = _maybe_start_recovery(agent)
                    if status == "recovery_started":
                        recoveries.append(agent.id)
        sess.commit()
        return {"ok": True, "agents_evaluated": len(agents),
                "alerts_fired": fired, "recoveries_started": recoveries,
                "autorecover_enabled": AUTORECOVER_ENABLED}
    except Exception:
        log.exception("docck.run_tick_evaluation failed")
        sess.rollback()
        return {"ok": False}
    finally:
        sess.close()


# ============================================================
# v1.1 — Auto-recovery state machine
# ============================================================
def _circuit_breaker_open(sess, agent_id: str) -> bool:
    """True if the breaker is tripped: manually, or >= CB_THRESHOLD failed
    sequences within CB_WINDOW_SECONDS."""
    cb = sess.get(DocckCircuitBreaker, agent_id)
    if cb is None:
        return False
    if cb.manually_tripped:
        # manually_tripped doubles as a silence-until timestamp holder in some
        # flows; treat as open if set.
        return True
    if cb.window_start and (datetime.utcnow() - cb.window_start).total_seconds() <= CB_WINDOW_SECONDS:
        return cb.failed_sequence_count >= CB_THRESHOLD
    return False


def _increment_breaker(sess, agent_id: str) -> None:
    cb = sess.get(DocckCircuitBreaker, agent_id)
    now = datetime.utcnow()
    if cb is None:
        cb = DocckCircuitBreaker(agent_id=agent_id, window_start=now, failed_sequence_count=1)
        sess.add(cb)
    else:
        # Reset the window if it's stale, else increment within it.
        if cb.window_start is None or (now - cb.window_start).total_seconds() > CB_WINDOW_SECONDS:
            cb.window_start = now
            cb.failed_sequence_count = 1
        else:
            cb.failed_sequence_count += 1
    sess.commit()


def _watchdog_post(agent: DocckAgent, path: str, payload: dict, timeout: int = 25) -> tuple[bool, dict]:
    """POST to the agent's watchdog /control/* endpoint with its bearer secret.
    Returns (ok, response_dict). Never raises.

    Routes through the SOCKS5 proxy (CENA_PROXY=socks5h://localhost:1055) when set,
    because docck runs on Render with USERSPACE Tailscale — outbound to a tailnet IP
    (the watchdogs at 100.x:8767) only works via the SOCKS proxy, not plain sockets.
    This is the same path sam_chat.py uses to reach the cena gateway. (Sam #1257:
    the first armed test exhausted because urllib bypassed the proxy + timed out.)"""
    secret = (os.getenv(agent.watchdog_secret_env_var) or "").strip()
    if not secret:
        return False, {"error": f"no secret in env {agent.watchdog_secret_env_var}"}
    url = agent.watchdog_url.rstrip("/") + path
    proxy = (os.getenv("CENA_PROXY") or "").strip() or None
    try:
        import httpx
        client_kwargs: dict = {"timeout": timeout}
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as c:
            resp = c.post(url, json=payload,
                          headers={"Authorization": f"Bearer {secret}"})
        ok = (resp.status_code // 100 == 2)
        try:
            return ok, resp.json()
        except Exception:
            return ok, {"http_status": resp.status_code, "body": resp.text[:200]}
    except Exception as e:  # noqa: BLE001
        return False, {"error": str(e)[:200]}


def _heartbeat_since(sess, agent_id: str, since: datetime) -> bool:
    """True if any heartbeat for this agent arrived after `since` — i.e. the
    agent recovered."""
    row = sess.execute(
        select(DocckHeartbeat).where(
            DocckHeartbeat.agent_id == agent_id,
            DocckHeartbeat.received_at > since,
        ).limit(1)
    ).scalar_one_or_none()
    return row is not None


def _reboot_is_safe(agent: DocckAgent) -> bool:
    """Guard before issuing reboot_machine. For aick (cena's machine), skip
    reboot if there's been recent ezCater webhook activity — a reboot mid-burst
    can interrupt in-flight order writes (aick #1206). docck checks the orders
    table for a webhook-ingested row in the last 90s. For other machines, allow."""
    if agent.machine_label != "AiCk":
        return True
    try:
        # Best-effort: any order updated in the last 90s suggests live webhook
        # traffic. Conservative — if the query fails, DON'T block reboot (the
        # circuit breaker + escalation already gate it).
        from app.models import Order
        cutoff = datetime.utcnow() - timedelta(seconds=90)
        s2 = SessionLocal()
        try:
            recent = s2.execute(
                select(Order).where(Order.updated_at > cutoff).limit(1)
            ).scalar_one_or_none()
            return recent is None
        finally:
            s2.close()
    except Exception:
        return True


def _record_step(sess, seq_id: int, idx: int, action: str, status: str,
                 payload: dict | None, resp: dict | None = None) -> None:
    sess.add(DocckRestartStep(
        sequence_id=seq_id, step_index=idx, action=action,
        started_at=datetime.utcnow(), status=status,
        payload=payload, watchdog_response=resp,
    ))
    sess.commit()


def _do_step_action(agent: DocckAgent, step: dict) -> tuple[bool, dict]:
    """Translate a restart-sequence step into a watchdog call."""
    action = step.get("action")
    if action == "restart_service":
        return _watchdog_post(agent, "/control/restart_service",
                              {"service_name": step.get("service_name")})
    if action == "restart_services":
        return _watchdog_post(agent, "/control/restart_services",
                              {"service_names": step.get("service_names", []),
                               "all_or_nothing": False})
    if action == "reboot_machine":
        if not ALLOW_REBOOT:
            return False, {"skipped": "reboot_gated_off (DOCCK_ALLOW_REBOOT not set) — service-restart-only mode"}
        if not _reboot_is_safe(agent):
            return False, {"skipped": "reboot_unsafe_recent_webhook_activity"}
        return _watchdog_post(agent, "/control/reboot_machine",
                              {"reason": f"docck auto-recovery for {agent.id}"})
    return False, {"error": f"unknown action {action!r}"}


def _execute_restart_sequence(agent_id: str) -> None:
    """Run an agent's restart sequence to completion (or recovery). Runs in its
    own thread because steps have long waits (30-360s). Records every step.
    Honors AUTORECOVER_ENABLED (dry-run when off)."""
    sess = SessionLocal()
    try:
        agent = sess.get(DocckAgent, agent_id)
        if agent is None:
            return
        seq = DocckRestartSequence(agent_id=agent_id, started_at=datetime.utcnow(),
                                   triggered_by="auto")
        sess.add(seq)
        sess.commit()
        seq_id = seq.id
        steps = agent.restart_sequence_json or []
        mode = "LIVE" if AUTORECOVER_ENABLED else "DRY-RUN"
        _post_alert_deduped(
            sess, agent_id, "warn", "dev_chat", f"{agent_id}_recovery_{seq_id}",
            f"[docck] {agent.display_name} recovery sequence #{seq_id} starting "
            f"({len(steps)} steps, {mode}).", dedupe_window_seconds=60,
        )

        for idx, step in enumerate(steps, start=1):
            action = step.get("action")
            wait_s = int(step.get("wait_seconds", 60))

            if not AUTORECOVER_ENABLED:
                # DRY-RUN: record intent, don't call the watchdog, don't wait long.
                _record_step(sess, seq_id, idx, action, "dry_run", step,
                             {"note": "AUTORECOVER disabled — no watchdog call made"})
                log.warning("[docck DRY-RUN] would execute step %d/%d for %s: %s",
                            idx, len(steps), agent_id, action)
                continue

            # LIVE execution
            ok, resp = _do_step_action(agent, step)
            _record_step(sess, seq_id, idx, action,
                         "issued" if ok else "watchdog_failure", step, resp)
            log.warning("[docck] %s step %d/%d %s -> ok=%s resp=%s",
                        agent_id, idx, len(steps), action, ok, resp)
            if not ok:
                continue  # advance to next escalation step

            mark = datetime.utcnow()
            time.sleep(wait_s)
            # Did the agent recover (fresh heartbeat after we acted)?
            if _heartbeat_since(sess, agent_id, mark):
                seq.ended_at = datetime.utcnow()
                seq.outcome = "recovered"
                seq.recovered_at_step = action
                sess.commit()
                _post_alert_deduped(
                    sess, agent_id, "info", "dev_chat", f"{agent_id}_recovered_{seq_id}",
                    f"[docck] {agent.display_name} RECOVERED after step {idx} ({action}), "
                    f"sequence #{seq_id}.", dedupe_window_seconds=60,
                )
                return

        # All steps exhausted without recovery → escalate
        seq.ended_at = datetime.utcnow()
        seq.outcome = "escalated"
        sess.commit()
        _increment_breaker(sess, agent_id)
        _post_alert_deduped(
            sess, agent_id, "urgent", "dev_chat", f"{agent_id}_escalated_{seq_id}",
            f"[docck] {agent.display_name} AUTO-RECOVERY EXHAUSTED (sequence #{seq_id}, {mode}). "
            f"All {len(steps)} steps ran, agent still not heartbeating. HUMAN NEEDED.",
            dedupe_window_seconds=300,
        )
    except Exception:
        log.exception("docck restart sequence for %s crashed", agent_id)
        sess.rollback()
    finally:
        sess.close()
        with _active_lock:
            _active_recoveries.pop(agent_id, None)


def _maybe_start_recovery(agent: DocckAgent) -> str:
    """Decide whether to launch a recovery sequence for a missing agent.
    Returns a short status string for logging. Gated by: circuit breaker,
    no-active-sequence, and a live thread guard. Spawns a thread on go."""
    sess = SessionLocal()
    try:
        if _circuit_breaker_open(sess, agent.id):
            return "breaker_open"
        # Already an unfinished sequence in the DB?
        active = sess.execute(
            select(DocckRestartSequence).where(
                DocckRestartSequence.agent_id == agent.id,
                DocckRestartSequence.ended_at.is_(None),
            ).limit(1)
        ).scalar_one_or_none()
        if active is not None:
            return "sequence_already_active"
    finally:
        sess.close()

    with _active_lock:
        if agent.id in _active_recoveries and _active_recoveries[agent.id].is_alive():
            return "thread_already_running"
        t = threading.Thread(target=_execute_restart_sequence, args=(agent.id,),
                             name=f"docck-recover-{agent.id}", daemon=True)
        _active_recoveries[agent.id] = t
        t.start()
    return "recovery_started"


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
