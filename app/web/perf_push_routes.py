"""Dedicated employee-performance perf-push receiver (Sam #3176 / #3177 / #3178).

A SEPARATED, self-contained ingest endpoint for the CK-local perf-DB push, created
to DECOUPLE the employee-performance path from the catering/driver system. The
legacy /cron/perf-push historically lived in app/web/driver_system.py (accidental
coupling) -- which also owns the completed catering/driver functionality. Per Sam
#3178 that coupling is FROZEN: this module is the dedicated home for the perf
ingest, importing ONLY from app.db / app.models, NEVER from driver_system.py or any
catering/driver route. The legacy /cron/perf-push stays untouched + unused by CK.

CK (Mini_IT13 = source of truth) POSTs SANITIZED per-period + per-shift + rank rows
for one employee; this upserts PerfPeriodCache / PerfShiftCache / PerfRankCache.
Stores ONLY known employee-visible keys (service -> service_json; INTERNAL
attribution -> attribution_json, a column the employee payload NEVER reads). No
sales field is ever accepted or stored.

Receiver-side guards (audited for GATE-3):
  - TOKEN-GATED, FAIL-CLOSED: CRON_TOKEN unset OR mismatch -> 403 (first statement).
  - WHOLE-BODY SALES-WALL (N-b, Sam #3028): reject the ENTIRE push (422) if any
    sales / eligible_sales / source-sales term appears ANYWHERE in the body. Only
    the tip% RATIO may ever be stored.
  - RANK PEER-ROW WHITELIST (N-c, Sam #3028): reject (422) any leaderboard row
    carrying a field outside RANK_PEER_FIELDS (fail-closed at store; guards a
    peer-pay/sales/GUID leak).
  - NO profile/link writes; NO Employee/User creation; perf caches only.
"""
from __future__ import annotations

import json as _json
import os
import re as _re
from datetime import datetime

from flask import Blueprint, abort, jsonify, request

from app.db import SessionLocal
from app.models import PerfPeriodCache, PerfShiftCache, PerfRankCache, rank_peer_rows_ok

perf_push_bp = Blueprint("perf_push", __name__)

# Whole-body server-side sales-wall (N-b): only the tip% RATIO may ever reach a
# cache; any sales / eligible_sales / source-sales token anywhere in the body -> 422.
_SALES_WALL = _re.compile(
    r"cashsales|noncashsales|eligible_sales|sales_attributed|sales_dollars|"
    r"salesattributed|salesdollars|eligiblesales|grosssales|netsales|sourcesales|checktotal|storetotal|"
    r"salesbasis|eligiblesalesbasis|eligible_sales_basis|source_sales|"
    r"\bsales\b|\bgross\b|\brevenue\b|\bdrawer\b|gratuityservicecharges|"
    r"ccsubtotal|cashamount|cc_subtotal|cash_amount|net_sales|check_total|store_total", _re.I)


_SERVICE_TIMING_KEYS = (
    "avg_drink_secs", "avg_app_secs", "avg_entree_secs",
    "avg_gap_secs", "avg_duration_secs",
)
_SERVICE_COUNT_KEYS = (
    "drink_count", "avg_drink_count", "drink_samples",
    "app_count", "avg_app_count", "app_samples",
    "entree_count", "avg_entree_count", "entree_samples",
    "gap_count", "avg_gap_count", "gap_samples",
    "duration_count", "avg_duration_count", "duration_samples",
)
_SERVICE_VISIBLE_KEYS = set(_SERVICE_TIMING_KEYS) | set(_SERVICE_COUNT_KEYS) | {
    "tickets", "tip_pct",
}


def _as_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _linked_guid_locations(emp, links):
    out = []
    seen = set()
    for link in links:
        guid = (getattr(link, "toast_id", None) or "").strip()
        if not guid:
            continue
        loc = (getattr(link, "store_key", None) or "").strip().lower() or None
        key = (guid, loc)
        if key not in seen:
            seen.add(key)
            out.append(key)
    profile_guid = (getattr(emp, "toast_employee_guid", None) or "").strip()
    if profile_guid and not any(guid == profile_guid for guid, _loc in out):
        out.append((profile_guid, None))
    return out


def _merge_employee_service_metrics(results):
    avg_fields = {
        "avg_drink_secs": "drink_count",
        "avg_app_secs": "app_count",
        "avg_entree_secs": "entree_count",
        "avg_gap_secs": "gap_count",
        "avg_duration_secs": "duration_count",
    }
    out = {}
    total_tickets = 0
    total_tips = 0.0
    total_subtotal = 0.0
    for res in results:
        if not isinstance(res, dict):
            continue
        total_tickets += int(_as_float(res.get("tickets")) or 0)
        total_tips += float(_as_float(res.get("_cc_tips")) or 0.0)
        total_subtotal += float(_as_float(res.get("_cc_subtotal")) or 0.0)
    if total_tickets:
        out["tickets"] = total_tickets
    for avg_key, count_key in avg_fields.items():
        numer = 0.0
        denom = 0
        for res in results:
            if not isinstance(res, dict):
                continue
            avg = _as_float(res.get(avg_key))
            count = int(_as_float(res.get(count_key)) or 0)
            if avg is None or count <= 0:
                continue
            numer += avg * count
            denom += count
        out[count_key] = denom
        out[avg_key] = (numer / denom) if denom > 0 else None
    out["tip_pct"] = (
        round(total_tips / total_subtotal * 100, 1)
        if total_subtotal > 0 else None
    )
    return {
        key: value for key, value in out.items()
        if key in _SERVICE_VISIBLE_KEYS and not str(key).startswith("_")
    }


def _extract_cron_token() -> str | None:
    """Self-contained token read (no import from driver_system, which is frozen):
    Authorization: Bearer <t>, or X-Cron-Token header, or ?token= query."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Cron-Token") or request.args.get("token")


@perf_push_bp.route("/cron/employee-perf-push", methods=["POST"])
def cron_employee_perf_push():
    """Token-gated, sales-wall-guarded receiver for the CK perf-DB push. Upserts one
    employee's SANITIZED PerfPeriodCache / PerfShiftCache / PerfRankCache. Isolated
    from driver/catering (Sam #3178). Body: {employee:{cena_employee_id, toast_id,
    store_key}, periods:[...], shifts:[...], rank:{...}}."""
    # FAIL-CLOSED (aick #3182): read expected ONCE; an UNSET/empty CRON_TOKEN must 403, not
    # fail-open. Without the `not expected` guard, os.getenv->None + a no-token request->None
    # makes None!=None False -> no abort -> unauthenticated push. (Matches perf_roster_link.py.)
    expected = os.getenv("CRON_TOKEN")
    if not expected or _extract_cron_token() != expected:
        abort(403)
    body = request.get_json(silent=True) or {}
    emp = body.get("employee") or {}
    cid = emp.get("cena_employee_id")
    if not cid:
        return jsonify({"ok": False, "error": "employee.cena_employee_id required"}), 400
    # N-b whole-body sales-wall (defense-in-depth): reject the entire push if any
    # sales / source-sales term appears ANYWHERE in the body.
    if _SALES_WALL.search(_json.dumps(body)):
        return jsonify({"ok": False, "error": "push body failed sales-wall guard"}), 422
    db = SessionLocal()
    written = 0
    try:
        for p in (body.get("periods") or []):
            per = (p.get("period") or "").strip()
            if not per:
                continue
            row = (db.query(PerfPeriodCache)
                     .filter_by(cena_employee_id=cid, period=per).first())
            if row is None:
                row = PerfPeriodCache(cena_employee_id=cid, period=per)
                db.add(row)
            row.toast_id = emp.get("toast_id")
            row.store_key = emp.get("store_key")
            row.period_start = p.get("period_start")
            row.period_end = p.get("period_end")
            row.total_hours = float(p.get("total_hours") or 0)
            row.reg_hours = float(p.get("reg_hours") or 0)
            row.ot_hours = float(p.get("ot_hours") or 0)
            row.base_pay = float(p.get("base_pay") or 0)
            row.tips = float(p.get("tips") or 0)
            # service_json carries the employee-visible course-timing (avg_*_secs).
            # The CK perf push does NOT compute timing, so it sends service={} -- if
            # we blindly overwrote, the every-minute today push would wipe the timing
            # that /cron/employee-service-timing-refresh writes. So: take the pushed
            # service ONLY when it actually carries timing; otherwise preserve the
            # existing row (default {} on first create). Sales-wall already cleared
            # the whole body above, so svc is safe to store.
            svc = p.get("service")
            visible_svc = (
                {
                    key: value for key, value in svc.items()
                    if key in _SERVICE_VISIBLE_KEYS and not str(key).startswith("_")
                }
                if isinstance(svc, dict) else {}
            )
            if visible_svc and (
                visible_svc.get("tip_pct") is not None
                or any(visible_svc.get(k) is not None for k in _SERVICE_TIMING_KEYS)
            ):
                row.service_json = visible_svc
            elif row.service_json is None:
                row.service_json = visible_svc
            # else: preserve the existing (refresh-owned) service_json
            attr = p.get("attribution")
            row.attribution_json = attr if isinstance(attr, dict) else None
            row.computed_at = p.get("computed_at")
            row.synced_at = datetime.utcnow()
            written += 1
        # per-shift -- same sanitize discipline; attribution -> internal column.
        shift_written = 0
        shifts = body.get("shifts") or []
        if shifts:
            db.query(PerfShiftCache).filter_by(cena_employee_id=cid).delete()
            for sh in shifts:
                row = PerfShiftCache(cena_employee_id=cid, clock_in=sh.get("clock_in"))
                row.toast_id = emp.get("toast_id")
                row.store_key = emp.get("store_key")
                row.business_date = sh.get("business_date")
                row.clock_out = sh.get("clock_out")
                row.reg_hours = float(sh.get("reg_hours") or 0)
                row.ot_hours = float(sh.get("ot_hours") or 0)
                row.total_hours = float(sh.get("total_hours") or 0)
                row.base_pay = float(sh.get("base_pay") or 0)
                row.tips = float(sh.get("tips") or 0)
                row.tips_declared = bool(sh.get("tips_declared", True))   # N4
                row.needs_review = bool(sh.get("needs_review", False))    # N5 (employee-visible flag)
                row.review_reason = sh.get("review_reason")
                attr = sh.get("attribution")
                row.attribution_json = attr if isinstance(attr, dict) else None
                db.add(row)
                shift_written += 1
        # Phase 5.1 ranking -- store the SANITIZED rank blob. N-c peer-row whitelist
        # backstop: reject (422) any leaderboard row with a field outside the allowed
        # set (fail-closed; guards a future peer-pay leak). The whole-body sales-wall
        # above already covers any sales token anywhere in the payload.
        rank_written = 0
        rank = body.get("rank")
        if isinstance(rank, dict):
            ok, offending = rank_peer_rows_ok(rank)
            if not ok:
                db.rollback()
                return jsonify({"ok": False, "error": "rank payload failed peer-row whitelist",
                                "offending_fields": offending}), 422
            rrow = db.query(PerfRankCache).filter_by(cena_employee_id=cid).first()
            if rrow is None:
                rrow = PerfRankCache(cena_employee_id=cid)
                db.add(rrow)
            rrow.rank_json = rank
            rrow.computed_at = rank.get("computed_at")
            rrow.synced_at = datetime.utcnow()
            rank_written = 1
        db.commit()
        return jsonify({"ok": True, "cena_employee_id": cid,
                        "periods_written": written, "shifts_written": shift_written,
                        "rank_written": rank_written}), 200
    finally:
        db.close()


@perf_push_bp.route("/cron/employee-service-timing-refresh", methods=["POST"])
def cron_employee_service_timing_refresh():
    """Compute employee-visible course-timing (avg drink/app/entree/gap/duration
    seconds) and tip percent from Toast and write it into
    PerfPeriodCache.service_json, so the employee Floor-Pulse "Technical
    averages" read from the DB instead of a live per-request Toast pull
    (Sam 2026-06-17). Run on a schedule on BOTH the local app and prod so each
    environment's DB is populated.

    Token-gated (CRON_TOKEN), same as the perf push. Sales-clean at rest: Toast
    subtotals/tip dollars are used only inside this request to calculate tip_pct;
    service_json stores only timing seconds/counts, ticket count, and tip_pct.
    Scoped to each employee's OWN confirmed Toast GUID(s), plus the employee
    profile Toast UUID fallback. Optional ?employee_id=<id> refreshes a single
    employee so a scheduler can fan out one-per-request and avoid a long request;
    with no arg it refreshes every employee that has cached periods.
    """
    expected = os.getenv("CRON_TOKEN")
    if not expected or _extract_cron_token() != expected:
        abort(403)

    from app.models import Employee
    from app.services.toast_identity import links_for_employee
    from app.services import toast_reports

    only = request.args.get("employee_id", type=int)
    db = SessionLocal()
    employees = 0
    periods_updated = 0
    errors = 0
    try:
        q = db.query(PerfPeriodCache.cena_employee_id).distinct()
        if only:
            q = q.filter(PerfPeriodCache.cena_employee_id == only)
        cids = [c for (c,) in q.all() if c]
        for cid in cids:
            emp = db.query(Employee).filter(Employee.id == cid).first()
            if emp is None:
                continue
            links = links_for_employee(db, emp)
            guid_locs = _linked_guid_locations(emp, links)
            if not guid_locs:
                continue
            employees += 1
            rows = db.query(PerfPeriodCache).filter_by(cena_employee_id=cid).all()
            changed = False
            for row in rows:
                if not row.period_start or not row.period_end:
                    continue
                try:
                    start_dt = datetime.strptime(row.period_start, "%Y-%m-%d")
                    end_dt = datetime.strptime(row.period_end, "%Y-%m-%d")
                    results = [
                        toast_reports.server_perf_metrics_for_guid(
                            start_dt,
                            end_dt,
                            guid,
                            loc,
                            include_private_totals=True,
                        ) or {}
                        for guid, loc in guid_locs
                    ]
                    svc = _merge_employee_service_metrics(results)
                except Exception:
                    errors += 1
                    continue
                if (
                    svc.get("tip_pct") is not None
                    or any(svc.get(k) is not None for k in _SERVICE_TIMING_KEYS)
                ):
                    merged = {
                        key: value for key, value in dict(row.service_json or {}).items()
                        if key in _SERVICE_VISIBLE_KEYS and not str(key).startswith("_")
                    }
                    merged.update(svc)
                    row.service_json = merged
                    periods_updated += 1
                    changed = True
            if changed:
                db.commit()
        return jsonify({"ok": True, "employees": employees,
                        "periods_updated": periods_updated, "errors": errors}), 200
    finally:
        db.close()
