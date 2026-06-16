"""Background Toast -> snapshot sync (Sam #2845).

The Link tab + the employee "My Hours & Pay" panel used to pull LIVE from Toast
on every page load -- the performance pull alone fires ~30 Toast calls per view,
so x many employees x reloads the workers choked -> Render 502s + on/off flicker.

This service moves the heavy Toast work OUT of the request path: a daemon poller
pulls every confirmed-linked Toast employee's labor/performance/pay in BULK every
~15 min and upserts ToastEmployeeSnapshot. The web endpoints then serve from that
snapshot (a fast DB read, zero live Toast). Toast creds are env vars in the app
(samai #2847), so this runs in-app with nothing copied anywhere.

Mirrors app.services.produce_ingest (daemon poller) + docck_monitor (idempotent
per-process start). Cross-worker double-runs are harmless: the upsert is keyed
(store_key, toast_id) and a freshness check skips a cycle if another worker just
refreshed. Also exposed as POST /cron/toast-sync (CRON_TOKEN) so samai can wire a
Render cron as belt-and-suspenders + for an immediate post-deploy seed.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

from app.db import SessionLocal

log = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 15 * 60     # bulk refresh cadence
MIN_FRESH_SECONDS = 12 * 60         # soft cross-worker dedup: skip if synced this recently
_INITIAL_DELAY_SECONDS = 30         # let boot finish before the first (heavy) pull

_started = False
_started_lock = threading.Lock()


def sync_toast_snapshots(only_store: str | None = None, reconcile_profiles: bool = True) -> dict:
    """Pull every distinct CONFIRMED (store_key, toast_id) link once via
    toast_employee_summary() and upsert ToastEmployeeSnapshot. Per-row commit so
    a partial failure still persists the good rows + a concurrent worker sees
    freshness quickly. Returns {total, synced, failed, profiles?}; never raises out."""
    from app.models import ToastEmployeeSnapshot
    from app.services.toast_employee_profiles import reconcile_toast_employee_profiles
    from app.services.toast_identity import identity_pairs_for_sync
    from app.web.toast_link_routes import toast_employee_summary

    profile_summary = None
    if reconcile_profiles:
        try:
            profile_summary = reconcile_toast_employee_profiles(only_store=only_store)
        except Exception as ex:
            profile_summary = {"error": f"{type(ex).__name__}: {ex}"}
            log.exception("toast-sync: profile reconciliation failed")

    db = SessionLocal()
    total = ok = failed = 0
    try:
        pairs = identity_pairs_for_sync(db, only_store=only_store)
        for store_key, toast_id in pairs:
            total += 1
            try:
                payload, _status = toast_employee_summary(store_key, toast_id)
            except Exception as ex:  # the helper shouldn't raise, but never let the loop die
                payload = {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
            row = (db.query(ToastEmployeeSnapshot)
                     .filter(ToastEmployeeSnapshot.store_key == store_key,
                             ToastEmployeeSnapshot.toast_id == toast_id)
                     .first())
            if row is None:
                row = ToastEmployeeSnapshot(store_key=store_key, toast_id=toast_id)
                db.add(row)
            is_ok = bool(payload.get("ok"))
            row.ok = is_ok
            row.error = None if is_ok else (str(payload.get("error") or "unknown"))[:400]
            row.hours = float(payload.get("hours") or 0)
            row.timecards_json = payload.get("timecards") or []
            row.performance_json = payload.get("performance") or {"available": False}
            row.payroll_json = payload.get("payroll") or {"available": False}
            row.synced_at = datetime.utcnow()
            try:
                db.commit()
            except Exception:
                db.rollback()  # e.g. a concurrent worker inserted the same (store,toast)
            ok, failed = (ok + 1, failed) if is_ok else (ok, failed + 1)
        log.info("toast-sync: refreshed %d snapshot(s) -- %d ok, %d failed", total, ok, failed)
        # key is "synced" (not "ok") so callers can spread this into a response
        # alongside an "ok": True success flag without a key collision.
        result = {"total": total, "synced": ok, "failed": failed}
        if profile_summary is not None:
            result["profiles"] = profile_summary
        return result
    finally:
        db.close()


def read_snapshot(store_key: str, toast_id: str) -> dict | None:
    """Return one cached snapshot shaped like toast_employee_summary()'s payload
    (+ synced_at), or None if it hasn't been synced yet. Pure DB read -- this is
    what the web endpoints call instead of touching Toast."""
    from app.models import ToastEmployeeSnapshot
    db = SessionLocal()
    try:
        row = (db.query(ToastEmployeeSnapshot)
                 .filter(ToastEmployeeSnapshot.store_key == store_key,
                         ToastEmployeeSnapshot.toast_id == toast_id)
                 .first())
        if row is None:
            return None
        return {
            "ok": bool(row.ok),
            "error": row.error,
            "hours": row.hours or 0,
            "timecards": row.timecards_json or [],
            "performance": row.performance_json or {"available": False},
            "payroll": row.payroll_json or {"available": False},
            "synced_at": row.synced_at.isoformat() if row.synced_at else None,
        }
    finally:
        db.close()


def snapshot_status() -> dict:
    """Compact health read of the snapshot table for the cron response: row
    count, ok vs failed, a sample error, and the freshest sync time -- so the
    Toast sync can be CONFIRMED or DIAGNOSED without DB/dashboard access (the
    page endpoints are auth-gated; this is token-gated). Sam #2845/#2840."""
    from app.models import ToastEmployeeSnapshot
    db = SessionLocal()
    try:
        rows = db.query(ToastEmployeeSnapshot).all()
        failed = [r for r in rows if not r.ok]
        synced = [r.synced_at for r in rows if r.synced_at]
        return {
            "total": len(rows),
            "ok": len(rows) - len(failed),
            "failed": len(failed),
            "sample_error": (failed[0].error if failed else None),
            "latest_synced_at": (max(synced).isoformat() if synced else None),
        }
    except Exception as e:  # table missing / DB error -> surface it, don't raise
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        db.close()


def _recently_synced(within_seconds: int) -> bool:
    """True if ANY snapshot was refreshed within `within_seconds` -- a soft
    cross-worker dedup so two workers' pollers don't both pull every cycle."""
    from app.models import ToastEmployeeSnapshot
    db = SessionLocal()
    try:
        latest = (db.query(ToastEmployeeSnapshot.synced_at)
                    .order_by(ToastEmployeeSnapshot.synced_at.desc())
                    .first())
        if not latest or not latest[0]:
            return False
        return (datetime.utcnow() - latest[0]) < timedelta(seconds=within_seconds)
    except Exception:
        return False
    finally:
        db.close()


def _loop():
    log.info("toast-sync poller started (interval %ds)", SYNC_INTERVAL_SECONDS)
    time.sleep(_INITIAL_DELAY_SECONDS)
    while True:
        try:
            if not _recently_synced(MIN_FRESH_SECONDS):
                sync_toast_snapshots()
        except Exception:
            log.exception("toast-sync loop iteration failed (continuing)")
        time.sleep(SYNC_INTERVAL_SECONDS)


def start_in_background() -> bool:
    """Start the poller daemon once per worker process (idempotent)."""
    global _started
    with _started_lock:
        if _started:
            return False
        _started = True
    threading.Thread(target=_loop, name="toast-sync", daemon=True).start()
    log.info("toast-sync background poller launched")
    return True


# Standalone entrypoint for an EXTERNAL scheduler (Sam #2853): a Render cron job
# running `python -m app.services.toast_sync [store]` does the heavy Toast pull in
# its OWN process, so the web dyno is never loaded by the sync. This is the safe
# replacement for the in-app poller (which is now off by default).
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    _only = sys.argv[1] if len(sys.argv) > 1 else None
    print("toast-sync (standalone):", sync_toast_snapshots(only_store=_only))
