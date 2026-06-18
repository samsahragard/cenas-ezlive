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
from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import Blueprint, abort, jsonify, request

from app.db import SessionLocal
from app.models import (
    PerfMetricDetailCache,
    PerfPeriodCache,
    PerfShiftCache,
    PerfRankCache,
    rank_peer_rows_ok,
)

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

_SERVICE_METRIC_META = {
    "avg_drink_secs": {
        "label": "Avg drink",
        "count_keys": ("drink_count", "avg_drink_count", "drink_samples"),
    },
    "avg_app_secs": {
        "label": "Avg apps",
        "count_keys": ("app_count", "avg_app_count", "app_samples"),
    },
    "avg_entree_secs": {
        "label": "Avg entree",
        "count_keys": ("entree_count", "avg_entree_count", "entree_samples"),
    },
    "avg_gap_secs": {
        "label": "Drink-entree gap",
        "count_keys": ("gap_count", "avg_gap_count", "gap_samples"),
    },
    "avg_duration_secs": {
        "label": "Avg duration",
        "count_keys": ("duration_count", "avg_duration_count", "duration_samples"),
    },
}


def _as_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _display_minutes(seconds):
    sec = _as_float(seconds)
    if sec is None or sec <= 0:
        return "--"
    return f"{round(sec / 60)}m"


def _store_day():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(os.getenv("APP_TZ", "America/Chicago"))).date()
    except Exception:
        return (datetime.utcnow() - timedelta(hours=5)).date()


def _range_windows():
    today = _store_day()
    week_start = today - timedelta(days=(today.weekday() + 1) % 7)
    last_week_start = week_start - timedelta(days=7)
    last_week_end = week_start - timedelta(days=1)
    month_start = today.replace(day=1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    return {
        "today": (today, today),
        "current_week": (week_start, today),
        "last_week": (last_week_start, last_week_end),
        "current_month": (month_start, today),
        "last_month": (last_month_start, last_month_end),
    }


def _shifts_for_range(shift_rows, lo, hi):
    lo_iso = lo.isoformat()
    hi_iso = hi.isoformat()
    return [
        s for s in shift_rows
        if getattr(s, "business_date", None)
        and lo_iso <= str(getattr(s, "business_date")) <= hi_iso
    ]


def _period_spec_from_shifts(period, lo, hi, shift_rows, guid_locs):
    rows = _shifts_for_range(shift_rows, lo, hi)
    guid, loc = guid_locs[0] if guid_locs else (None, None)
    return SimpleNamespace(
        period=period,
        period_start=lo.isoformat(),
        period_end=hi.isoformat(),
        total_hours=round(sum(float(x.total_hours or 0) for x in rows), 2),
        reg_hours=round(sum(float(x.reg_hours or 0) for x in rows), 2),
        ot_hours=round(sum(float(x.ot_hours or 0) for x in rows), 2),
        base_pay=round(sum(float(x.base_pay or 0) for x in rows), 2),
        tips=round(sum(float(x.tips or 0) for x in rows), 2),
        toast_id=guid,
        store_key=loc,
        service_json={},
        computed_at=None,
    )


def _period_refresh_specs(period_rows, shift_rows, guid_locs):
    by_period = {getattr(r, "period", None): r for r in period_rows}
    specs = []
    used = set()
    for period, (lo, hi) in _range_windows().items():
        row = by_period.get(period)
        if (
            row is None
            or str(getattr(row, "period_start", "") or "") != lo.isoformat()
            or str(getattr(row, "period_end", "") or "") != hi.isoformat()
        ):
            row = _period_spec_from_shifts(period, lo, hi, shift_rows, guid_locs)
        specs.append(row)
        used.add(period)
    for row in period_rows:
        period = getattr(row, "period", None)
        if period and period not in used and getattr(row, "period_start", None) and getattr(row, "period_end", None):
            specs.append(row)
    return specs


def _metric_count(metrics, meta):
    for key in meta.get("count_keys", ()):
        val = _as_float((metrics or {}).get(key))
        if val is not None:
            return int(val)
    return 0


def _service_metric_detail(row, metric_key, metrics):
    meta = _SERVICE_METRIC_META[metric_key]
    label = meta["label"]
    seconds = _as_float((metrics or {}).get(metric_key))
    count = _metric_count(metrics, meta)
    tickets = int(_as_float((metrics or {}).get("tickets")) or 0)
    display = _display_minutes(seconds)
    range_label = f"{getattr(row, 'period_start', None) or '--'} to {getattr(row, 'period_end', None) or '--'}"
    rows = [
        {"label": "Range", "value": range_label, "source": "Performance DB"},
        {"label": "Toast samples", "value": str(count), "source": "Operations"},
    ]
    if tickets:
        rows.append({"label": "Tickets", "value": str(tickets), "source": "Operations"})
    if seconds is None or seconds <= 0:
        formula = (
            f"{label} needs Toast timing samples for this employee and period. "
            "The Performance DB has no usable timing average for this range yet."
        )
        return {
            "label": label,
            "value": None,
            "display": "--",
            "unit": "minutes",
            "source": "Performance DB",
            "formula": formula,
            "count": count,
            "rows": rows,
        }
    if count:
        formula = (
            f"Performance DB stores Toast's latest {label.lower()} average as "
            f"{round(seconds)} seconds across {count} sample"
            f"{'' if count == 1 else 's'}; {round(seconds)} seconds / 60 = {display}."
        )
    else:
        formula = (
            f"Performance DB stores Toast's latest {label.lower()} average as "
            f"{round(seconds)} seconds; {round(seconds)} seconds / 60 = {display}."
        )
    return {
        "label": label,
        "value": round(seconds, 2),
        "display": display,
        "unit": "minutes",
        "source": "Performance DB",
        "formula": formula,
        "count": count,
        "rows": rows,
    }


def _tip_pct_detail(row, metrics):
    tip_pct = _as_float((metrics or {}).get("tip_pct"))
    tickets = int(_as_float((metrics or {}).get("tickets")) or 0)
    display = f"{tip_pct:.1f}%" if tip_pct is not None and tip_pct > 0 else "--"
    range_label = f"{getattr(row, 'period_start', None) or '--'} to {getattr(row, 'period_end', None) or '--'}"
    rows = [
        {"label": "Range", "value": range_label, "source": "Performance DB"},
    ]
    if tickets:
        rows.append({"label": "Tickets", "value": str(tickets), "source": "Operations"})
    formula = (
        f"Performance DB stores Toast's latest credit-card tip percentage for this period as {display}."
        if display != "--"
        else "The Performance DB has no valid Toast credit-card tip percentage for this range yet."
    )
    return {
        "label": "Total tip %",
        "value": round(tip_pct, 2) if tip_pct is not None and tip_pct > 0 else None,
        "display": display,
        "unit": "percent",
        "source": "Performance DB",
        "formula": formula,
        "rows": rows,
    }


def _hours_detail(row, shift_rows):
    try:
        lo = datetime.strptime(str(getattr(row, "period_start")), "%Y-%m-%d").date()
        hi = datetime.strptime(str(getattr(row, "period_end")), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        rows = []
    else:
        rows = _shifts_for_range(shift_rows, lo, hi)
    detail_rows = []
    for s in rows:
        detail_rows.append({
            "date": getattr(s, "business_date", None),
            "clock_in": getattr(s, "clock_in", None),
            "clock_out": getattr(s, "clock_out", None),
            "hours": round(float(getattr(s, "total_hours", 0) or 0), 2),
            "source": "Clocked shift",
        })
    hours = round(float(getattr(row, "total_hours", 0) or 0), 2)
    parts = [f"{round(float(x.get('hours') or 0), 2):g}" for x in detail_rows]
    if parts:
        shown = " + ".join(parts[:8])
        if len(parts) > 8:
            shown += f" + {len(parts) - 8} more"
        formula = (
            f"Hours = sum of {len(detail_rows)} clock row"
            f"{'' if len(detail_rows) == 1 else 's'} stored in Performance DB: "
            f"{shown} = {hours:.2f}h."
        )
    else:
        formula = "No clock rows are posted in Performance DB for this employee and range yet."
    return {
        "label": "Hours",
        "value": hours,
        "display": f"{hours:.1f}h",
        "unit": "hours",
        "source": "Performance DB clock rows",
        "formula": formula,
        "rows": detail_rows,
    }


def _upsert_metric_detail(db, cid, row, metric_key, detail):
    cache = (
        db.query(PerfMetricDetailCache)
        .filter_by(cena_employee_id=cid, period=getattr(row, "period"), metric_key=metric_key)
        .first()
    )
    if cache is None:
        cache = PerfMetricDetailCache(
            cena_employee_id=cid,
            period=getattr(row, "period"),
            metric_key=metric_key,
        )
        db.add(cache)
    cache.period_start = getattr(row, "period_start", None)
    cache.period_end = getattr(row, "period_end", None)
    cache.value = detail.get("value")
    cache.display = detail.get("display")
    cache.source = detail.get("source")
    cache.formula = detail.get("formula")
    cache.detail_json = {
        key: detail.get(key)
        for key in ("label", "unit", "count", "rows")
        if detail.get(key) is not None
    }
    cache.computed_at = (
        getattr(row, "computed_at", None)
        or datetime.utcnow().isoformat(timespec="seconds") + "Z"
    )
    cache.synced_at = datetime.utcnow()


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
    PerfPeriodCache.service_json + PerfMetricDetailCache, so the employee
    Floor-Pulse "Performance" cards read from the DB instead of a live
    per-click Toast pull
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
    detail_rows_updated = 0
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
            shift_rows = db.query(PerfShiftCache).filter_by(cena_employee_id=cid).all()
            refresh_specs = _period_refresh_specs(rows, shift_rows, guid_locs)
            metrics_by_range = {}
            changed = False
            for row in refresh_specs:
                if not row.period_start or not row.period_end:
                    continue
                try:
                    range_key = (row.period_start, row.period_end)
                    if range_key in metrics_by_range:
                        svc = metrics_by_range[range_key]
                    else:
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
                        metrics_by_range[range_key] = svc
                except Exception:
                    errors += 1
                    continue
                if (
                    svc.get("tip_pct") is not None
                    or any(svc.get(k) is not None for k in _SERVICE_TIMING_KEYS)
                ):
                    if isinstance(row, PerfPeriodCache):
                        merged = {
                            key: value for key, value in dict(row.service_json or {}).items()
                            if key in _SERVICE_VISIBLE_KEYS and not str(key).startswith("_")
                        }
                        merged.update(svc)
                        row.service_json = merged
                        periods_updated += 1
                        changed = True
                for metric_key in _SERVICE_METRIC_META:
                    _upsert_metric_detail(
                        db, cid, row, metric_key,
                        _service_metric_detail(row, metric_key, svc),
                    )
                    detail_rows_updated += 1
                    changed = True
                _upsert_metric_detail(db, cid, row, "tip_pct", _tip_pct_detail(row, svc))
                detail_rows_updated += 1
                _upsert_metric_detail(db, cid, row, "hours", _hours_detail(row, shift_rows))
                detail_rows_updated += 1
                changed = True
            if changed:
                db.commit()
        return jsonify({"ok": True, "employees": employees,
                        "periods_updated": periods_updated,
                        "detail_rows_updated": detail_rows_updated,
                        "errors": errors}), 200
    finally:
        db.close()
