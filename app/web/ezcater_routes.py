# API endpoints for upload/extract/breakdown
from __future__ import annotations

import io
import logging
from pathlib import Path
from uuid import uuid4
import threading
import time

from flask import Blueprint, current_app, render_template, request, send_file, jsonify, redirect, url_for, g
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

import os

from app.services.orders_service import process_and_export, process_single_pdf
from app.services.persistence_service import persist_processing_job, persist_results

cater = Blueprint("ezcater", __name__)

_jobs: dict[str, dict] = {}
_JOB_TTL_SECONDS = 3600

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# Desktop dashboard marquee banner — one illuminated-marquee graphic per
# role, shown in place of the "Welcome back" text header (Sam 2026-05-20).
# Keys cover BOTH the legacy User.permission_level values (partner /
# corporate / gm / manager / expo / corporate-driver — see app/models.py
# User docstring) AND the newer canonical taxonomy from
# app/services/permissions.py ROLE_PERMISSIONS + app/services/role_hierarchy.py
# (km / assistant_km / corporate_chef / prep_manager / foh_manager / driver
# / cook / server / busser / host / bartender). Files live at
# app/static/brand/banners/<value>.jpg. Any role with no specific banner —
# and any unknown / None role — falls back to 'general'.
_ROLE_BANNERS: dict[str, str] = {
    "partner":         "partner",
    "corporate_chef":  "chef",
    "gm":              "manager",
    "manager":         "manager",      # legacy permission_level value
    "foh_manager":     "manager",
    "km":              "kitchen_manager",
    "assistant_km":    "kitchen_manager",
    "prep_manager":    "prep",
    "expo":            "expo",
    "driver":          "driver",
    "corporate-driver": "driver",      # legacy permission_level value
}
_DEFAULT_BANNER = "general"


def _dashboard_banner_for(user) -> str:
    """Static filename stem of the marquee banner for ``user``'s role.

    Reads only ``user.permission_level``; unknown roles, and 'corporate'
    (an admin tier with no role-specific marquee), fall through to the
    'general' banner. Never raises."""
    role = (getattr(user, "permission_level", None) or "").strip()
    return _ROLE_BANNERS.get(role, _DEFAULT_BANNER)


def _run_job(app, job_id: str, pdf_paths: list[str], collapse_empty_rows: bool):
    with app.app_context():
        job_db_id = None
        try:
            job_db_id = persist_processing_job(len(pdf_paths))
            result = process_and_export(pdf_paths, collapse_empty_rows=collapse_empty_rows)
            persist_results(job_db_id, result.get("orders", []))
            _jobs[job_id].update({"status": "done", "pdf_count": len(pdf_paths), "result": result})
        except Exception as e:
            _jobs[job_id].update({"status": "failed", "pdf_count": len(pdf_paths), "result": None, "error": str(e)})
        finally:
            _cleanup_files(pdf_paths)


MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30 MB


def _save_uploaded_pdfs(files) -> list[str]:
    upload_dir = Path(current_app.root_path).parent / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[str] = []

    for f in files:
        if not f or not f.filename:
            continue

        filename = secure_filename(f.filename)
        if not filename.lower().endswith(".pdf"):
            continue

        unique_name = f"{uuid4().hex}_{filename}"
        path = upload_dir / unique_name
        f.save(path)

        file_size = path.stat().st_size
        if file_size > MAX_UPLOAD_BYTES:
            path.unlink()
            logger.warning("Rejected oversized file: %s (%.1f MB)", filename, file_size / 1024 / 1024)
            continue

        saved_paths.append(str(path))

    return saved_paths


def _cleanup_files(paths: list[str]) -> None:
    for path_str in paths:
        try:
            path = Path(path_str)
            if path.exists():
                path.unlink()
        except Exception as e:
            logger.warning("File cleanup failed for %s: %s", path_str, e)

def _evict_stale_jobs():
    cutoff = time.time() - _JOB_TTL_SECONDS
    stale = [jid for jid, j in list(_jobs.items()) if j.get("created_at", 0) < cutoff]
    for jid in stale:
        del _jobs[jid]


def home():
    """Manager dashboard. Pulls today's deliveries + attention items live
    from the DB so a manager opening the site sees the agenda first, not
    just navigation.

    Note: the route registration for this view moved out — `/` now serves
    the store picker. The `/<store>/` URL prefix layer (store_routes.py)
    calls this function directly after setting g.current_location."""
    from datetime import datetime, timezone, timedelta
    from pathlib import Path
    import json
    from app.db import get_db
    from app.models import Order

    # Render runs UTC; without an explicit offset, today_iso flips to
    # tomorrow after 7pm CT and the dashboard shows the wrong day's deliveries.
    now_ct = datetime.now(timezone(timedelta(hours=-5)))
    today_iso = now_ct.strftime("%Y-%m-%d")
    # Per-store filtering: Tomball = stores 2/4, Copperfield = stores 1/3
    location = getattr(g, "current_location", "both")
    tomball_stores = ("store_2", "store_4")
    copperfield_stores = ("store_1", "store_3")

    def _decorate_order(o):
        origin = o.origin_store_id or ""
        loc = "Tomball" if origin in tomball_stores else "Copperfield"
        if not (o.client and o.client.strip()):
            badge_class, badge_text = "badge-warn", "No customer"
        elif o.assigned_driver:
            badge_class, badge_text = "badge-good", "On track"
        else:
            badge_class, badge_text = "badge-info", "Unassigned"
        sub_bits = []
        if o.assigned_driver:
            sub_bits.append(f"Driver: {o.assigned_driver}")
        if o.headcount:
            sub_bits.append(f"{o.headcount} heads")
        if o.setup_required:
            sub_bits.append("Setup required")
        return {
            "order_id": o.external_order_id,
            "time": o.deliver_at or "—",
            "name": (o.client or "").strip() or f"{loc} delivery",
            "sub": " · ".join(sub_bits),
            "location": loc,
            "badge_class": badge_class,
            "badge_text": badge_text,
        }

    def _time_sort_key(t):
        # deliver_at is stored as text like "11:00 AM" / "5:45 AM"; a plain
        # lexicographic sort places "5:45 AM" after "12:00 PM". Parse to
        # minutes-since-midnight so the within-day order is real wall-clock.
        if not t:
            return 99999
        try:
            dt = datetime.strptime(t.strip(), "%I:%M %p")
            return dt.hour * 60 + dt.minute
        except Exception:
            return 99998

    db = next(get_db())
    try:
        # Strict today — used for KPI tiles and the attention list below.
        today_q = (
            db.query(Order)
            .filter(Order.delivery_date == today_iso)
            .filter(Order.status != "cancelled")
        )
        if location == "tomball":
            today_q = today_q.filter(Order.origin_store_id.in_(tomball_stores))
        elif location == "copperfield":
            today_q = today_q.filter(Order.origin_store_id.in_(copperfield_stores))
        today_orders = today_q.all()
        # Review queue retired 2026-05-10 — auto-resolver + Telegram replaces it.
        review_orders = []

        # KPI counts
        tomball_today = sum(1 for o in today_orders if (o.origin_store_id or "") in tomball_stores)
        copperfield_today = len(today_orders) - tomball_today
        heads_today = sum((o.headcount or 0) for o in today_orders)

        # Upcoming (today + future) for the "Upcoming deliveries" card.
        upcoming_q = (
            db.query(Order)
            .filter(Order.delivery_date >= today_iso)
            .filter(Order.status != "cancelled")
        )
        if location == "tomball":
            upcoming_q = upcoming_q.filter(Order.origin_store_id.in_(tomball_stores))
        elif location == "copperfield":
            upcoming_q = upcoming_q.filter(Order.origin_store_id.in_(copperfield_stores))
        upcoming_orders = upcoming_q.all()
        upcoming_orders.sort(key=lambda o: (o.delivery_date or "", _time_sort_key(o.deliver_at)))

        # Group by delivery_date. Always seed Today so the card renders
        # "Today — no deliveries" when empty; future days only appear
        # if they have orders. Insertion order = today first, then
        # ascending future dates (upcoming_orders is pre-sorted).
        upcoming_groups = {today_iso: {"date_iso": today_iso, "label": "Today", "orders": []}}
        for o in upcoming_orders:
            d = o.delivery_date or today_iso
            if d not in upcoming_groups:
                try:
                    dt = datetime.strptime(d, "%Y-%m-%d")
                    label = f"{dt.strftime('%A')} {dt.month}/{dt.day}/{str(dt.year)[2:]}"
                except Exception:
                    label = d
                upcoming_groups[d] = {"date_iso": d, "label": label, "orders": []}
            upcoming_groups[d]["orders"].append(_decorate_order(o))
        upcoming_groups_list = list(upcoming_groups.values())

        # Attention list. Review-queue items removed (auto-resolver handles
        # extraction warnings now via Telegram alerts). Still flag today's
        # orders missing a customer name as those are user-facing data
        # quality issues kitchen needs to know about.
        attention = []
        # Today's orders missing a customer name
        for o in today_orders:
            if not (o.client and o.client.strip()):
                origin = o.origin_store_id or ""
                loc = "Tomball" if origin in ("store_2", "store_4") else "Copperfield"
                attention.append({
                    "kind": "warn",
                    "text": f"{o.external_order_id} missing customer name",
                    "meta": f"{loc} · {o.deliver_at or 'time TBD'} · review before kitchen prep",
                })
                if len(attention) >= 5:
                    break

    finally:
        db.close()

    # Produce winners + last-refresh
    produce_state_dir = Path(os.getenv("PRODUCE_STATE_DIR")
                             or (Path(__file__).resolve().parents[2] / "instance" / "produce"))
    alvarado = {}
    jluna = {}
    try:
        af = produce_state_dir / "alvarado.json"
        if af.exists():
            alvarado = json.loads(af.read_text(encoding="utf-8"))
        jf = produce_state_dir / "jluna.json"
        if jf.exists():
            jluna = json.loads(jf.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("could not read produce state for dashboard")
    produce_winners = len({(it.get("canonical_name"), it.get("canonical_size"))
                           for it in (alvarado.get("items") or []) + (jluna.get("items") or [])
                           if it.get("canonical_name")})
    last_parsed = max(filter(None, [alvarado.get("parsed_at"), jluna.get("parsed_at")]),
                      default=None)
    last_parsed_short = ""
    if last_parsed:
        try:
            dt = datetime.fromisoformat(last_parsed.replace("Z", "+00:00"))
            last_parsed_short = dt.strftime("%b %d, %I:%M %p").replace(" 0", " ").lstrip("0")
        except Exception:
            last_parsed_short = last_parsed[:16]

    # Stale-produce attention item
    if last_parsed:
        try:
            from datetime import timezone
            dt = datetime.fromisoformat(last_parsed.replace("Z", "+00:00"))
            now_utc = datetime.now(timezone.utc)
            age_days = (now_utc - dt).days
            if age_days >= 5:
                attention.append({
                    "kind": "info",
                    "text": f"Produce prices are {age_days} days old",
                    "meta": "Vendor email overdue — site shows stale data",
                })
        except Exception:
            pass

    today_long = now_ct.strftime("%A, %B %d").replace(" 0", " ")

    return render_template(
        "home.html",
        today_iso=today_iso,
        today_long=today_long,
        upcoming_groups=upcoming_groups_list,
        attention=attention[:5],
        tomball_today=tomball_today,
        copperfield_today=copperfield_today,
        heads_today=heads_today,
        review_count=len(review_orders),
        produce_winners=produce_winners,
        produce_last_refresh=last_parsed_short,
        dashboard_location=location,   # 'tomball' / 'copperfield' / 'both' for the JS fetcher
        dashboard_banner=_dashboard_banner_for(getattr(g, "current_user", None)),
    )


@cater.route("/dashboard/summary", methods=["GET"])
def dashboard_summary():
    """JSON feed powering the Sales + Labor boxes at the top of every dashboard.

    Query params:
        period   = today | week | prev_week  (default 'today')
        location = both | tomball | copperfield  (default 'both')

    Returns net sales, labor cost / hours / ratio for the requested window.
    Net sales come from Toast (sum of pre-tax non-voided check.amount across
    all channels — in-store + online + DoorDash + Toast Local). ezCater
    catering revenue is NOT included yet (Order DB has headcount but no
    per-order total).
    """
    from flask import request, jsonify
    from datetime import datetime, timedelta, timezone
    from app.services import toast_reports

    period = (request.args.get("period") or "today").lower()
    location = (request.args.get("location") or "both").lower()
    if location not in ("both", "tomball", "copperfield"):
        return jsonify({"error": f"invalid location {location!r}"}), 400

    # Render runs in UTC; restaurant is in Central Time. "Today" must be the
    # restaurant's calendar date or Toast's businessDate lookup misses data.
    CT = timezone(timedelta(hours=-5))
    today = datetime.now(CT).date()
    if period == "today":
        start = end = today
        label = today.strftime("%a, %b %d").replace(" 0", " ")
    elif period == "week":
        # Current week: Sun → today (Sam 2026-05-11 — week starts on Sunday).
        # Offset back to most-recent Sun is (weekday()+1) % 7 days.
        start = today - timedelta(days=(today.weekday() + 1) % 7)
        end = today
        label = f"{start.strftime('%b %d')} – {end.strftime('%b %d')}".replace(" 0", " ")
    elif period == "prev_week":
        # Last full Sun → Sat (week ending the most recent Saturday).
        this_sun = today - timedelta(days=(today.weekday() + 1) % 7)
        end = this_sun - timedelta(days=1)
        start = end - timedelta(days=6)
        label = f"{start.strftime('%b %d')} – {end.strftime('%b %d')}".replace(" 0", " ")
    else:
        return jsonify({"error": f"invalid period {period!r}"}), 400

    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())
    loc_filter = None if location == "both" else location

    try:
        labor_rep = toast_reports.labor_report(start_dt, end_dt, location_filter=loc_filter)
        sales_rep = toast_reports.third_party_sales_report(
            start_dt, end_dt, location_filter=loc_filter, channel_filter="total"
        )
    except Exception as e:
        logger.exception("dashboard_summary: toast fetch failed")
        return jsonify({
            "error": "toast_fetch_failed",
            "detail": str(e)[:200],
            "period": period, "location": location,
        }), 502

    # Sales: total + per-channel donut slices
    sales_total = float((labor_rep.get("totals") or {}).get("net_sales") or 0.0)
    sales_channels = []
    for c in sales_rep.get("by_channel") or []:
        sales_channels.append({
            "key": c.get("key"),
            "label": c.get("label"),
            "value": float(c.get("sales") or 0.0),
            "orders": int(c.get("orders") or 0),
        })
    # Drop empty/$0 slices and re-sort biggest first
    sales_channels = sorted(
        [c for c in sales_channels if c["value"] > 0],
        key=lambda r: -r["value"],
    )

    # Labor: source = Toast ANALYTICS API (the /era/v1/* endpoints). The
    # legacy labor_report path uses Toast Web API's /labor/v1/timeEntries
    # which only includes CLOSED shifts — drastically under-counts during
    # active service hours. Analytics gives us Toast's pre-aggregated daily
    # totals including in-progress shifts, matching what the Toast Web UI
    # shows. Per Sam 2026-05-12.
    from app.services.role_classifier import classify_role
    from app.services.toast_analytics_client import ToastAnalyticsClient, ToastAnalyticsError
    _LOC_TO_GUID = {
        "tomball":     os.getenv("TOAST_RESTAURANT_GUID_TOMBALL", ""),
        "copperfield": os.getenv("TOAST_RESTAURANT_GUID_COPPERFIELD", ""),
    }
    if location == "both":
        ana_restaurant_ids = [g for g in _LOC_TO_GUID.values() if g]
    elif location in _LOC_TO_GUID and _LOC_TO_GUID[location]:
        ana_restaurant_ids = [_LOC_TO_GUID[location]]
    else:
        ana_restaurant_ids = []  # empty = all accessible
    start_ymd = start.strftime("%Y%m%d")
    end_ymd = end.strftime("%Y%m%d")
    labor_total = 0.0
    labor_hours = 0.0
    labor_roles: list[dict] = []
    labor_warnings: list[str] = []
    try:
        ana = ToastAnalyticsClient.shared()
        metrics_rows = ana.metrics(start_ymd, end_ymd, ana_restaurant_ids)
        labor_total = sum(float(m.get("hourlyJobTotalPay") or 0) for m in metrics_rows)
        labor_hours = sum(float(m.get("hourlyJobTotalHours") or 0) for m in metrics_rows)
        # BOH/FOH split via classify_role on the by-job analytics rows.
        labor_by_job = ana.labor(start_ymd, end_ymd, ana_restaurant_ids, group_by=["JOB"])
        boh_cost = foh_cost = 0.0
        for row in labor_by_job:
            cost = float(row.get("totalCost") or 0)
            role = classify_role(row.get("jobTitle") or "")
            if role == "boh":
                boh_cost += cost
            else:
                foh_cost += cost
        if boh_cost > 0:
            labor_roles.append({"key": "boh", "label": "BOH (Kitchen)", "value": round(boh_cost, 2)})
        if foh_cost > 0:
            labor_roles.append({"key": "foh", "label": "FOH (Service)", "value": round(foh_cost, 2)})
    except ToastAnalyticsError as e:
        logger.exception("dashboard_summary: Toast Analytics labor fetch failed; falling back to labor_report")
        labor_warnings.append("Toast Analytics labor failed; numbers may be lower (in-progress shifts excluded).")
        # Fall back to the old labor_report path so the dashboard still
        # renders something useful even when the Analytics API is down.
        boh_cost = foh_cost = 0.0
        for row in labor_rep.get("by_position") or []:
            cost = row.get("labor_cost")
            if cost is None:
                pct = row.get("pct_net_sales") or 0.0
                cost = (pct / 100.0) * sales_total
            role = classify_role(row.get("title") or "")
            if role == "boh":
                boh_cost += cost
            else:
                foh_cost += cost
        labor_total = float((labor_rep.get("totals") or {}).get("labor_cost") or 0.0)
        labor_hours = float((labor_rep.get("totals") or {}).get("hours") or 0.0)
        if labor_total <= 0 and (boh_cost + foh_cost) > 0:
            labor_total = boh_cost + foh_cost
        if boh_cost > 0:
            labor_roles.append({"key": "boh", "label": "BOH (Kitchen)", "value": boh_cost})
        if foh_cost > 0:
            labor_roles.append({"key": "foh", "label": "FOH (Service)", "value": foh_cost})

    labor_ratio_pct = (labor_total / sales_total * 100.0) if sales_total > 0 else 0.0

    return jsonify({
        "period": period,
        "label": label,
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "location": location,
        "sales": {
            "total": sales_total,
            "by_channel": sales_channels,
        },
        "labor": {
            "total_cost": round(labor_total, 2),
            "hours": round(labor_hours, 2),
            "shifts": int((labor_rep.get("totals") or {}).get("shifts") or 0),
            "ratio_pct": round(labor_ratio_pct, 1),
            "by_role": labor_roles,
        },
        "warnings": (labor_rep.get("warnings") or []) + labor_warnings,
    })


@cater.route("/dashboard/analytics-summary", methods=["GET"])
def dashboard_analytics_summary():
    """JSON feed for the Toast-Analytics-only donut block on the Partner
    dashboard. Different API + scope from /dashboard/summary above:
      - /dashboard/summary uses the standard Toast Web/Partner API (both
        stores)
      - /dashboard/analytics-summary uses the new Analytics API (Copperfield
        only — Tomball is not on the RMS Pro plan)

    Query params:
        period   = today | week | last_week (default 'today')

    Reference: /partner/developer/app/toast-analytics-api
    """
    from flask import request, jsonify
    from app.services.toast_analytics_client import (
        ToastAnalyticsClient, ToastAnalyticsError, period_to_ymd_range,
    )

    period = (request.args.get("period") or "today").lower()
    if period not in ("today", "week", "last_week"):
        return jsonify({"error": f"invalid period {period!r}"}), 400

    try:
        start_ymd, end_ymd, label = period_to_ymd_range(period)
        client = ToastAnalyticsClient.shared()

        metrics = client.metrics(start_ymd, end_ymd, [])
        labor_rows = client.labor(start_ymd, end_ymd, [], group_by=["JOB"])
        menu_rows = client.menu(start_ymd, end_ymd, [])
    except ToastAnalyticsError as e:
        logger.exception("dashboard_analytics_summary: analytics fetch failed")
        return jsonify({
            "error": "toast_analytics_fetch_failed",
            "detail": str(e)[:240],
            "period": period,
        }), 502
    except Exception as e:
        logger.exception("dashboard_analytics_summary: unexpected error")
        return jsonify({"error": "unexpected", "detail": str(e)[:240]}), 500

    # ---- sales totals (sum across days/restaurants) ----
    net_sales = sum(float(m.get("netSalesAmount") or 0) for m in metrics)
    gross_sales = sum(float(m.get("grossSalesAmount") or 0) for m in metrics)
    discount_amt = sum(float(m.get("discountAmount") or 0) for m in metrics)
    void_amt = sum(float(m.get("voidOrdersAmount") or 0) for m in metrics)
    refund_amt = sum(float(m.get("refundAmount") or 0) for m in metrics)
    orders_count = sum(int(m.get("ordersCount") or 0) for m in metrics)
    guest_count = sum(int(m.get("guestCount") or 0) for m in metrics)
    labor_hours = sum(float(m.get("hourlyJobTotalHours") or 0) for m in metrics)
    labor_pay = sum(float(m.get("hourlyJobTotalPay") or 0) for m in metrics)
    avg_order = (net_sales / orders_count) if orders_count else 0.0
    sales_per_labor_hour = (net_sales / labor_hours) if labor_hours else 0.0
    labor_ratio_pct = (labor_pay / net_sales * 100.0) if net_sales else 0.0

    # ---- labor mix by job (sum cost per job title) ----
    by_job: dict[str, dict] = {}
    for row in labor_rows:
        title = (row.get("jobTitle") or "Other").strip() or "Other"
        if title not in by_job:
            by_job[title] = {"label": title, "value": 0.0, "hours": 0.0}
        by_job[title]["value"] += float(row.get("totalCost") or 0)
        by_job[title]["hours"] += float(row.get("totalHours") or 0)
    labor_by_job = sorted(
        [v for v in by_job.values() if v["value"] > 0],
        key=lambda r: -r["value"],
    )

    # ---- menu summary (sum across days) ----
    menu_qty = sum(float(r.get("quantitySold") or 0) for r in menu_rows)
    menu_avg_price = (
        sum(float(r.get("netSalesAmount") or 0) for r in menu_rows) / menu_qty
        if menu_qty else 0.0
    )
    menu_waste_amount = sum(float(r.get("wasteAmount") or 0) for r in menu_rows)
    menu_waste_count = sum(float(r.get("wasteCount") or 0) for r in menu_rows)

    # restaurant scope note — analytics token can see both but data only
    # flows for RMS-Pro-subscribed locations. Today: Copperfield only.
    restaurants_in_data = sorted({m.get("restaurantGuid") for m in metrics if m.get("restaurantGuid")})
    scope_note = (
        "Copperfield only — Tomball is not on the Toast Analytics plan."
        if len(restaurants_in_data) <= 1 else
        f"{len(restaurants_in_data)} locations included."
    )

    return jsonify({
        "period": period,
        "label": label,
        "date_range": {"start": start_ymd, "end": end_ymd},
        "scope_note": scope_note,
        "sales": {
            "net": round(net_sales, 2),
            "gross": round(gross_sales, 2),
            "discount": round(discount_amt, 2),
            "void": round(void_amt, 2),
            "refund": round(refund_amt, 2),
            "avg_order": round(avg_order, 2),
            "sales_per_labor_hour": round(sales_per_labor_hour, 2),
            "orders": orders_count,
            "guests": guest_count,
        },
        "labor": {
            "hours": round(labor_hours, 2),
            "cost": round(labor_pay, 2),
            "ratio_pct": round(labor_ratio_pct, 1),
            "by_job": [
                {"label": r["label"], "value": round(r["value"], 2), "hours": round(r["hours"], 2)}
                for r in labor_by_job
            ],
        },
        "menu": {
            "quantity_sold": round(menu_qty, 0),
            "avg_price": round(menu_avg_price, 2),
            "waste_amount": round(menu_waste_amount, 2),
            "waste_count": round(menu_waste_count, 0),
        },
    })


@cater.route("/orders", methods=["GET", "POST"])
def orders():
    if request.method == "GET":
        return render_template(
            "orders.html",
            grids=None,
            orders=[],
            active_view="master",
            collapse_empty_rows=True,
            error=None,
            success_count=0,
            failure_count=0,
            xlsx_job_id=None,
        )

    files = request.files.getlist("pdfs")
    if not files:
        return render_template(
            "orders.html",
            grids=None,
            orders=[],
            active_view="master",
            collapse_empty_rows=request.form.get("collapse_empty_rows") == "1",
            error="No files were uploaded.",
            success_count=0,
            failure_count=0,
            xlsx_job_id=None,
        )

    pdf_paths = _save_uploaded_pdfs(files)
    job_id = uuid4().hex
    collapse_empty_rows = request.form.get("collapse_empty_rows") == "1"
    _evict_stale_jobs()
    _jobs[job_id] = {"status": "processing", "pdf_count": len(pdf_paths), "result": None, "error": None, "created_at": time.time()}

    t = threading.Thread(
        target=_run_job,
        args=(current_app._get_current_object(), job_id, pdf_paths, collapse_empty_rows),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@cater.route("/download/job/<job_id>")
def download_job(job_id):
    job = _jobs.get(job_id)
    if not job or job["status"] != "done":
        return "Job not found or not complete", 404
    result = job.get("result") or {}
    xlsx_bytes = result.get("xlsx_bytes")
    if not xlsx_bytes:
        return "No export available for this job", 404
    collapse = result.get("collapse_empty_rows", False)
    filename = "ezcater_orders_collapsed.xlsx" if collapse else "ezcater_orders.xlsx"
    return send_file(
        io.BytesIO(xlsx_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype=XLSX_MIME,
    )


@cater.route("/export", methods=["POST"])
def export_orders():
    pdf_paths: list[str] = []

    try:
        files = request.files.getlist("pdfs")
        if not files:
            return render_template(
                "orders.html",
                grids=None,
                orders=[],
                active_view="master",
                collapse_empty_rows=False,
                error="No files were uploaded for export.",
                success_count=0,
                failure_count=0,
                xlsx_job_id=None,
            )

        pdf_paths = _save_uploaded_pdfs(files)
        if not pdf_paths:
            return render_template(
                "orders.html",
                grids=None,
                orders=[],
                active_view="master",
                collapse_empty_rows=False,
                error="No valid PDF files were uploaded for export.",
                success_count=0,
                failure_count=0,
                xlsx_job_id=None,
            )

        collapse_empty_rows = request.form.get("collapse_empty_rows") == "1"
        result = process_and_export(pdf_paths, collapse_empty_rows=collapse_empty_rows)

        if not result.get("success") or not result.get("xlsx_bytes"):
            return render_template(
                "orders.html",
                grids=result.get("grids"),
                orders=result.get("orders", []),
                active_view="master",
                collapse_empty_rows=collapse_empty_rows,
                error=result.get("error", "Export failed."),
                success_count=result.get("success_count", 0),
                failure_count=result.get("failure_count", 0),
                xlsx_job_id=None,
            )

        filename = "ezcater_orders_collapsed.xlsx" if collapse_empty_rows else "ezcater_orders.xlsx"
        return send_file(
            io.BytesIO(result["xlsx_bytes"]),
            as_attachment=True,
            download_name=filename,
            mimetype=XLSX_MIME,
        )

    except Exception as e:
        logger.error("Export route failed: %s", e, exc_info=True)
        return render_template(
            "orders.html",
            grids=None,
            orders=[],
            active_view="master",
            collapse_empty_rows=request.form.get("collapse_empty_rows") == "1",
            error=f"Unexpected export error: {str(e)}",
            success_count=0,
            failure_count=0,
            xlsx_job_id=None,
        )
    finally:
        _cleanup_files(pdf_paths)


@cater.route("/orders/status/<job_id>/poll")
def poll_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    payload = {"status": job["status"]}
    if job["status"] == "done":
        result = job["result"]
        payload["success_count"] = result.get("success_count", 0)
        payload["failure_count"] = result.get("failure_count", 0)
    elif job["status"] == "failed":
        payload["error"] = job.get("error", "Unknown error")
    return jsonify(payload)


@cater.route("/orders/ingest_structured", methods=["POST"])
def ingest_order_structured():
    """Auto-ingest endpoint that takes a structured RawOrder JSON instead
    of a PDF. Used by the ezCater Partner API helper on AiCk — no Claude
    vision step, the API already returns clean structured data.

    Auth: Bearer token in Authorization header, matching INGEST_TOKEN env.
    Body: JSON in the RawOrder shape (see app/domain/schemas.py:RawOrder).
    """
    import time as _time
    started = _time.time()

    expected = os.getenv("INGEST_TOKEN")
    if not expected:
        return jsonify({"error": "INGEST_TOKEN not configured on server"}), 500
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != expected:
        return jsonify({"error": "unauthorized"}), 401

    raw_order = request.get_json(silent=True)
    if not isinstance(raw_order, dict):
        return jsonify({"error": "expected JSON RawOrder body"}), 400

    # Required-field gate (mirrors what the Claude path enforces in pdf_reader).
    # Use truthy check for string fields (empty == missing) but presence-check
    # for headcount (0 or null is valid — some ezCater orders genuinely have
    # no headcount, e.g. small drop-offs).
    required_truthy = ("order_id", "store", "date", "deliver_at", "delivery_address")
    missing = [k for k in required_truthy if not raw_order.get(k)]
    if "headcount" not in raw_order:
        missing.append("headcount")
    if missing:
        return jsonify({"error": f"missing required fields: {', '.join(missing)}"}), 400
    if not isinstance(raw_order.get("raw_items"), list) or len(raw_order["raw_items"]) == 0:
        return jsonify({"error": "raw_items empty or not a list"}), 400

    # Run the same downstream pipeline the PDF flow runs after extraction.
    from app.domain.validation import validate_raw_order, validate_normalized_order
    from app.domain.normalize import normalize_order
    from app.domain.kitchen_engine import build_kitchen_result
    from app.domain.ticket_context import build_ticket_context
    from app.domain.master_sheet_map import build_all_outputs
    from app.services.dispatch_planner import build_dispatch_plans
    from app.services.orders_service import catalog as _catalog

    raw_warnings = validate_raw_order(raw_order)
    try:
        normalized = normalize_order(raw_order, _catalog)
    except Exception as e:
        logger.exception("normalize failed for structured ingest")
        return jsonify({"success": False, "stage": "normalizing_order", "error": str(e)}), 422

    norm_warnings = validate_normalized_order(normalized)
    all_warnings = raw_warnings + norm_warnings

    try:
        kitchen_result = build_kitchen_result(normalized)
    except Exception as e:
        logger.exception("kitchen rules failed")
        return jsonify({"success": False, "stage": "building_result", "error": str(e)}), 422

    try:
        dispatch_plans = build_dispatch_plans([normalized])
    except Exception as e:
        logger.warning("dispatch failed for structured ingest %s: %s", normalized.get("order_id"), e)
        dispatch_plans = {}
    dispatch = dispatch_plans.get(normalized.get("order_id"), {})
    normalized["route_group_id"] = dispatch.get("route_group_id")
    normalized["route_stop_index"] = dispatch.get("route_stop_index")
    normalized["assigned_driver"] = dispatch.get("assigned_driver")

    try:
        ctx = build_ticket_context(normalized, kitchen_result, dispatch)
        views = build_all_outputs(normalized, kitchen_result, ctx, _catalog)
    except Exception as e:
        logger.exception("post-processing failed")
        return jsonify({"success": False, "stage": "post_processing", "error": str(e)}), 422

    bundle = {
        "success": True,
        "pdf_path": "",  # no PDF for this path
        "order_id": normalized.get("order_id"),
        "raw_order": raw_order,
        "normalized_order": normalized,
        "kitchen_result": kitchen_result,
        "ticket_context": ctx,
        "views": views,
        "dispatch": dispatch,
        "warnings": all_warnings,
        "needs_review": bool(all_warnings),
        "processing_seconds": round(_time.time() - started, 2),
        # Pass-through ezCater identifiers for the unassign-courier flow.
        "external_delivery_id": raw_order.get("_external_delivery_id"),
    }

    job_db_id = persist_processing_job(1)
    persist_results(job_db_id, [bundle])

    return jsonify({
        "success": True,
        "order_id": normalized.get("order_id"),
        "needs_review": bundle["needs_review"],
        "warnings": all_warnings,
        "view_url": url_for("orders_browse.view_order",
                            external_order_id=normalized.get("order_id"),
                            _external=False),
        "processing_seconds": bundle["processing_seconds"],
    }), 200


@cater.route("/orders/ingest", methods=["POST"])
def ingest_order():
    """Auto-ingest endpoint for the AiCk ezcater agent. Accepts a single PDF
    and runs it through the same pipeline as /orders, but synchronously and
    without the browser-driven async job dance.

    Auth: Bearer token in Authorization header, matching INGEST_TOKEN env.
    Loopback usage only — token is not strong enough for public exposure
    (cloudflared tunnel routes /orders/ingest the same as anything else,
    so don't share the token).
    """
    expected = os.getenv("INGEST_TOKEN")
    if not expected:
        return jsonify({"error": "INGEST_TOKEN not configured on server"}), 500
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != expected:
        return jsonify({"error": "unauthorized"}), 401

    files = request.files.getlist("pdf")
    if not files or not files[0].filename:
        return jsonify({"error": "no pdf uploaded (expected multipart field 'pdf')"}), 400
    if len(files) > 1:
        return jsonify({"error": "only one pdf per request"}), 400

    pdf_paths = _save_uploaded_pdfs(files)
    if not pdf_paths:
        return jsonify({"error": "pdf rejected (size or extension)"}), 400

    pdf_path = pdf_paths[0]
    try:
        result = process_single_pdf(pdf_path)
        if not result.get("success"):
            return jsonify({
                "success": False,
                "stage": result.get("stage"),
                "error": result.get("error"),
            }), 422

        # Mirror the post-processing the multi-PDF flow does (dispatch + ticket
        # context). For a single ingest we don't bother with pairing — solo
        # dispatch only.
        from app.services.dispatch_planner import build_dispatch_plans
        from app.domain.ticket_context import build_ticket_context
        from app.domain.master_sheet_map import build_all_outputs
        from app.services.orders_service import catalog as _catalog

        normalized = result["normalized_order"]
        kitchen_result = result["kitchen_result"]
        try:
            dispatch_plans = build_dispatch_plans([normalized])
        except Exception as e:
            logger.warning("dispatch failed for ingested %s: %s", normalized.get("order_id"), e)
            dispatch_plans = {}
        dispatch = dispatch_plans.get(normalized.get("order_id"), {})
        normalized["route_group_id"] = dispatch.get("route_group_id")
        normalized["route_stop_index"] = dispatch.get("route_stop_index")
        normalized["assigned_driver"] = dispatch.get("assigned_driver")
        ctx = build_ticket_context(normalized, kitchen_result, dispatch)
        views = build_all_outputs(normalized, kitchen_result, ctx, _catalog)

        bundle = {
            **result,
            "ticket_context": ctx,
            "views": views,
            "dispatch": dispatch,
        }

        job_db_id = persist_processing_job(1)
        persist_results(job_db_id, [bundle])

        return jsonify({
            "success": True,
            "order_id": normalized.get("order_id"),
            "needs_review": result.get("needs_review", False),
            "warnings": result.get("warnings", []),
            "view_url": url_for("orders_browse.view_order",
                                external_order_id=normalized.get("order_id"),
                                _external=False),
        }), 200
    finally:
        _cleanup_files(pdf_paths)


@cater.route("/orders/result/<job_id>")
def job_result(job_id):
    job = _jobs.get(job_id)
    if not job or job["status"] != "done":
        return redirect(url_for("ezcater.orders"))
    result = job["result"]
    has_xlsx = result.get("success") and result.get("xlsx_bytes")
    collapse = result.get("collapse_empty_rows", False)
    return render_template(
        "orders.html",
        grids=result.get("grids"),
        orders=result.get("orders", []),
        active_view="master",
        collapse_empty_rows=collapse,
        error=result.get("error"),
        success_count=result.get("success_count", 0),
        failure_count=result.get("failure_count", 0),
        xlsx_job_id=job_id if has_xlsx else None,
    )
