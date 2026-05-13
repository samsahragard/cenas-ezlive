"""Phase 1 / Block 5 — anomaly rule library.

Implements the 30 rules samai cataloged in
app/templates/docs/anomaly_rules.html sections 3.1–3.9
(2 of the original 32 — orders.no_driver_30min_before and
orders.late_delivery — already shipped in
app/services/anomaly_engine.py as seed rules during Block 1).

Approach: register every rule with @anomaly_rule so the REGISTRY is
complete (ck's /partner/anomalies/rules admin shows them all, cron
buckets dispatch them properly, samai's review can verify the full
list). Rule BODIES are split into two camps:

  - Implemented:  the rule has a working SQL/API query against
                  data we already have in production.

  - Stub:         the rule's data source doesn't exist yet (no
                  ProduceDispute / KDS / vendor_quotes / Google
                  Reviews / Sling clock-in tables in our schema).
                  Body returns [] and the docstring + TODO comment
                  names the blocking data source. The rule still
                  occupies a slot in REGISTRY so the admin and
                  morning-brief composer treat it as known.

Bringing a stub to life later = swap the body for a real query.
No engine, cron, or admin code needs to change.

Import side-effect registers all rules in REGISTRY at module load.
app/__init__.py imports this module so the engine sees them on boot.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Cancellation, Driver, Order
from app.services.anomaly_engine import SignalDraft, anomaly_rule

logger = logging.getLogger(__name__)


# Shared origin-store-id → slug mapping for store_id field on SignalDrafts.
def _origin_to_store_slug(origin_store_id: str | None) -> str | None:
    return {
        "store_1": "uno", "store_3": "uno",
        "store_2": "dos", "store_4": "dos",
    }.get(origin_store_id or "")


# ============================================================
# 3.1 Vendor / produce (6 rules)
# ============================================================

@anomaly_rule(
    name="vendor.invoice_over_quoted_price",
    bucket="on_write", severity="warn",
    surfaces=["produce.queue", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm", "km", "corporate_chef"],
    action_text="Dispute the variance with the vendor. Open /produce/dispute pre-filled with the line.",
)
def vendor_invoice_over_quoted_price(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: needs vendor_quotes table + invoice ingest event hook.
    # ProducePriceSnapshot stores per-day vendor prices but not the
    # "quoted weekly" anchor against which actuals are compared. Stub.
    return []


@anomaly_rule(
    name="vendor.invoice_missing_line_items",
    bucket="on_write", severity="warn",
    surfaces=["produce.queue", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm", "km", "corporate_chef"],
    action_text="Vendor short-shipped — verify with kitchen receiver and request a follow-up delivery or credit.",
)
def vendor_invoice_missing_line_items(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: needs purchase_orders table to compare against
    # invoice line items. Stub.
    return []


@anomaly_rule(
    name="vendor.late_delivery",
    bucket="every_15m", severity="alert",
    surfaces=["produce.queue", "home", "kds.station",
              "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm", "km", "corporate_chef", "prep_manager"],
    action_text="Call vendor immediately. If >1h late, escalate to backup vendor.",
)
def vendor_late_delivery(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: needs vendors.delivery_window config + a way to
    # detect "no invoice for today by window-end". produce_ingest writes
    # alvarado/jluna JSONs without timestamps suitable for this check yet.
    return []


@anomaly_rule(
    name="vendor.spec_price_anomaly_yoy",
    bucket="weekly", severity="info",
    surfaces=["produce.queue", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "km", "corporate_chef"],
    action_text="Pricing trend up — consider re-quoting with other vendors or hedging supply.",
)
def vendor_spec_price_anomaly_yoy(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: ProducePriceSnapshot exists. Needs at least 1 year
    # of snapshots for the YoY compare; today we have ~30 days. When
    # snapshots cross 365 days, swap the body to compare current week
    # avg vs same-week-last-year avg, fire on >15% increase. Stub.
    return []


@anomaly_rule(
    name="produce.missed_order_window",
    bucket="hourly", severity="alert",
    surfaces=["produce.queue", "home", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm", "km", "corporate_chef"],
    action_text="Order is now LATE. Place by phone or move to next delivery window; flag impact on prep.",
)
def produce_missed_order_window(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: needs vendor order-by deadlines config (alvarado
    # Sun 8pm CT, jluna Wed 2pm CT) + record of orders placed against
    # the current cycle. produce_order blueprint has submit but no
    # window-tracking yet. Stub.
    return []


@anomaly_rule(
    name="produce.dispute_unresolved",
    bucket="daily", severity="warn",
    surfaces=["produce.queue", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm", "km"],
    action_text="Follow up with vendor on the open dispute. Convert to credit memo or escalate if vendor unresponsive.",
)
def produce_dispute_unresolved(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: needs ProduceDispute table (status='open', opened_at).
    # Trigger: opened_at < now() - 48h. Stub.
    return []


# ============================================================
# 3.2 Orders / delivery (3 of 5 — other 2 in anomaly_engine.py seeds)
# ============================================================

_DRIVER_NO_SHOW_GRACE = timedelta(minutes=15)


@anomaly_rule(
    name="orders.driver_no_show",
    bucket="every_5m", severity="alert",
    surfaces=["orders.by_store", "home",
              "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm", "foh_manager", "expo"],
    action_text="Reassign the order — either back to Ez Market or to a backup driver. Log the no-show against the driver.",
)
def orders_driver_no_show(db: Session) -> list[SignalDraft]:
    """assigned_driver IS NOT NULL AND pickup_at + 15min < now() AND
    mark_picked_up_at IS NULL. Reversible: clears once picked_up or
    re-assigned."""
    now = datetime.utcnow()
    cutoff = now - _DRIVER_NO_SHOW_GRACE
    rows = (
        db.query(Order)
        .filter(Order.status == "approved")
        .filter(Order.assigned_driver_id.isnot(None))
        .filter(Order.delivery_window_start.isnot(None))
        .filter(Order.delivery_window_start < cutoff)
        .filter(Order.pickup_actual_at.is_(None))
        .all()
    )
    out: list[SignalDraft] = []
    for o in rows:
        late = int((now - o.delivery_window_start).total_seconds() // 60)
        out.append(SignalDraft(
            rule_name="orders.driver_no_show",
            severity="alert",
            store_id=_origin_to_store_slug(o.origin_store_id),
            subject_id=o.external_order_id or str(o.id),
            subject_label=f"Order {o.external_order_id or o.id}",
            payload={
                "minutes_past_pickup": late,
                "assigned_driver_id": o.assigned_driver_id,
                "delivery_address": o.delivery_address,
            },
            action_text=(f"Driver {late} min past pickup window without picking up — "
                         "reassign or contact immediately."),
            surfaces=["orders.by_store", "home",
                      "partner.anomalies", "morning_brief"],
            audience_roles=["partner", "gm", "foh_manager", "expo"],
        ))
    return out


@anomaly_rule(
    name="orders.ezcater_rejection_rate",
    bucket="hourly", severity="warn",
    surfaces=["orders.by_store", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm"],
    action_text="Review the rejection reasons. Adjust accept-window or fix menu mapping if structural.",
)
def orders_ezcater_rejection_rate(db: Session) -> list[SignalDraft]:
    """Store rejected >= 3 ezCater orders in a rolling 24h window."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    # We count Cancellation rows tagged 'rejected' (cancelled_by != 'driver').
    # Today we record Cancellation on driver cancellation only — when
    # the rejection-side gets wired, this fires correctly. For now the
    # filter is broad: cancelled in the last 24h AND not driver-initiated.
    rows = (
        db.query(Cancellation)
        .filter(Cancellation.cancelled_at > cutoff)
        .filter(Cancellation.cancelled_by != "driver")
        .all()
    )
    # Group by store via the delivery
    by_store: dict[str, list[Cancellation]] = {}
    for c in rows:
        order = db.get(Order, c.delivery_id)
        if not order:
            continue
        slug = _origin_to_store_slug(order.origin_store_id)
        if slug:
            by_store.setdefault(slug, []).append(c)
    out: list[SignalDraft] = []
    for slug, cxs in by_store.items():
        if len(cxs) < 3:
            continue
        out.append(SignalDraft(
            rule_name="orders.ezcater_rejection_rate",
            severity="warn",
            store_id=slug,
            subject_id=f"{slug}-24h",
            subject_label=f"{slug.upper()} rejection rate (24h)",
            payload={
                "count_24h": len(cxs),
                "reasons": [c.reason for c in cxs[:5] if c.reason],
            },
            action_text=("Review rejection reasons; adjust accept-window or "
                         "fix menu mapping if structural."),
            surfaces=["orders.by_store", "partner.anomalies", "morning_brief"],
            audience_roles=["partner", "corporate", "gm"],
        ))
    return out


@anomaly_rule(
    name="orders.high_pickup_lag",
    bucket="daily", severity="info",
    surfaces=["orders.by_store", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm"],
    action_text="Driver fleet running slow — consider opening the bid pool earlier or recruiting more drivers.",
)
def orders_high_pickup_lag(db: Session) -> list[SignalDraft]:
    """Per store: trailing 5 completed orders' average minutes between
    approved_at and pickup_actual_at > 25 min."""
    # Per-store: pull last 5 delivered with both timestamps.
    out: list[SignalDraft] = []
    for slug, origins in (("uno", ("store_1", "store_3")),
                          ("dos", ("store_2", "store_4"))):
        rows = (
            db.query(Order)
            .filter(Order.origin_store_id.in_(origins))
            .filter(Order.status == "delivered")
            .filter(Order.approved_at.isnot(None))
            .filter(Order.pickup_actual_at.isnot(None))
            .order_by(Order.pickup_actual_at.desc())
            .limit(5)
            .all()
        )
        if len(rows) < 5:
            continue
        lags = [
            (o.pickup_actual_at - o.approved_at).total_seconds() / 60
            for o in rows
        ]
        avg = sum(lags) / len(lags)
        if avg <= 25:
            continue
        out.append(SignalDraft(
            rule_name="orders.high_pickup_lag",
            severity="info",
            store_id=slug,
            subject_id=f"{slug}-trailing-5",
            subject_label=f"{slug.upper()} avg pickup lag (last 5)",
            payload={
                "avg_minutes": round(avg, 1),
                "sample_size": len(rows),
                "order_ids": [o.external_order_id or str(o.id) for o in rows],
            },
            action_text=(f"Trailing-5 avg pickup lag is {avg:.0f} min — "
                         "consider opening the bid pool earlier."),
            surfaces=["orders.by_store", "partner.anomalies", "morning_brief"],
            audience_roles=["partner", "gm"],
        ))
    return out


# ============================================================
# 3.3 Sales (4 rules) — all stubs until Toast Analytics wiring lands
# ============================================================

@anomaly_rule(
    name="sales.daily_vs_forecast",
    bucket="daily", severity="warn",
    surfaces=["home", "reports.third_party_sales",
              "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm", "foh_manager"],
    action_text="Slow day — consider a happy-hour push or check if a competitor / event is drawing traffic.",
)
def sales_daily_vs_forecast(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: Toast Analytics /era/v1/metrics for today's sales
    # vs trailing 4-week same-DOW average run-rate at 2pm CT. The
    # client (app/services/toast_analytics_client.py) exists; needs a
    # SalesForecast / DowAverage cache + the comparison logic. Stub.
    return []


@anomaly_rule(
    name="sales.channel_divergence",
    bucket="weekly", severity="warn",
    surfaces=["reports.third_party_sales",
              "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm"],
    action_text="Channel underperforming — check ratings / wait time / menu freshness for that channel.",
)
def sales_channel_divergence(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: per-channel weekly sales from toast_reports.third_
    # party_sales_report vs trailing 4-week per-channel avg. Need to
    # persist weekly snapshots; today we compute on-demand only. Stub.
    return []


@anomaly_rule(
    name="sales.refund_spike",
    bucket="hourly", severity="warn",
    surfaces=["home", "reports.third_party_sales",
              "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm", "foh_manager"],
    action_text="Investigate refund reasons via Toast back-office; surface to KM or FOH per pattern.",
)
def sales_refund_spike(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: Toast Analytics voids + refund-class transactions
    # endpoint. Threshold: today's refunds > 2% of gross sales. Stub.
    return []


@anomaly_rule(
    name="sales.weekly_pace_divergence",
    bucket="weekly", severity="info",
    surfaces=["home", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm"],
    action_text="Soft week — consider whether to extend specials or pull marketing levers.",
)
def sales_weekly_pace_divergence(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: WTD sales vs trailing-4-week WTD avg per weekday.
    # Same Toast Analytics dependency. Stub.
    return []


# ============================================================
# 3.4 Labor (4 rules)
# ============================================================

@anomaly_rule(
    name="labor.percent_running_hot",
    bucket="hourly", severity="warn",
    surfaces=["home", "reports.labor", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm"],
    action_text="Send a clock-out wave home if FOH; revisit schedule for the rest of the week if pattern persists.",
)
def labor_percent_running_hot(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: today's labor-to-sales ratio > 115% of target.
    # Both sides need Toast Analytics (sales) + Sling (labor) on a
    # per-store rolling-today basis. Stub for now; toast_analytics +
    # sling_reports exist as the inputs, just need the comparison
    # against a stored target. Only fires after 6pm CT. Stub.
    return []


@anomaly_rule(
    name="labor.overtime_exceeded",
    bucket="weekly", severity="warn",
    surfaces=["reports.labor", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm"],
    action_text="Adjust the employee's schedule for the rest of the pay period. Surface to scheduling if structural.",
)
def labor_overtime_exceeded(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: per-employee weekly hours from Toast time entries;
    # fire on >48h. toast_analytics_client.labor endpoint returns
    # tracked hours per employee per period — needs the >48 threshold
    # check + per-employee Signal emission. Stub.
    return []


@anomaly_rule(
    name="labor.unscheduled_clockin",
    bucket="every_15m", severity="info",
    surfaces=["reports.labor", "partner.anomalies"],
    audience_roles=["gm", "foh_manager", "km"],
    action_text="Confirm the employee was needed today. If not, ask them to clock out.",
)
def labor_unscheduled_clockin(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: cross-reference Sling scheduled shifts vs Toast
    # clock-ins with a 15-min grace window. sling_reports.schedule
    # gives the shift list; toast_analytics_client.labor returns
    # clock-in times. Stub.
    return []


@anomaly_rule(
    name="labor.manager_absent",
    bucket="every_15m", severity="alert",
    surfaces=["home", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm", "km", "foh_manager"],
    action_text="Confirm coverage. If unplanned, contact the missing manager and arrange backup.",
)
def labor_manager_absent(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: no manager (km/gm/foh_manager/asst_km) clocked in
    # by 30 min after the first front-line clock-in. Needs Sling roles
    # tagged on employees + Toast clock-in detection. Stub.
    return []


# ============================================================
# 3.5 Attendance (2 rules) — both Sling-shift-dependent stubs
# ============================================================

@anomaly_rule(
    name="attendance.employee_pattern",
    bucket="weekly", severity="warn",
    surfaces=["reports.labor", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm"],
    action_text="Coaching conversation. Document in manager_log. Decide if progressive discipline applies.",
)
def attendance_employee_pattern(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: 2+ NCNS in 30 days per employee. Needs scheduled
    # shifts vs actual clock-ins + time-off requests. Stub.
    return []


@anomaly_rule(
    name="attendance.callout_spike",
    bucket="hourly", severity="warn",
    surfaces=["home", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm", "km", "foh_manager"],
    action_text="Find coverage from cross-trained staff. Alert corporate_chef for kitchen-side backup.",
)
def attendance_callout_spike(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: > 2 callouts on the same shift-day (time-off filed
    # within 4h of start OR no-show). Needs Sling time-off feed. Stub.
    return []


# ============================================================
# 3.6 Server performance (1 rule)
# ============================================================

@anomaly_rule(
    name="server_perf.tip_rate_low",
    bucket="weekly", severity="info",
    surfaces=["reports.server_perf", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm", "foh_manager"],
    action_text="Coaching opportunity — review service patterns, table assignments, pair with high-tip server.",
)
def server_perf_tip_rate_low(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: per-server trailing-5-shift avg tip% < 12%. Min
    # qualifying shifts = 5. toast_reports.server_perf_report already
    # computes per-server tip rates; needs a "trailing-5" window
    # accessor. Stub.
    return []


# ============================================================
# 3.7 Kitchen (4 rules) — all KDS-dependent stubs
# ============================================================

@anomaly_rule(
    name="kitchen.prep_behind",
    bucket="on_write", severity="warn",
    surfaces=["kds.station", "home", "partner.anomalies"],
    audience_roles=["partner", "gm", "km", "prep_manager"],
    action_text="Shift bodies onto the lagging station. Escalate to recipe-yield or schedule if structural.",
)
def kitchen_prep_behind(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: KDS prep-station progress vs rolling production
    # target. No KDS schema yet. Stub.
    return []


@anomaly_rule(
    name="kitchen.recipe_yield_variance",
    bucket="on_write", severity="info",
    surfaces=["kds.station", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "km", "corporate_chef"],
    action_text="Investigate — recipe update? ingredient quality? prep process drift?",
)
def kitchen_recipe_yield_variance(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: actual yield < 90% target for a prepped recipe.
    # No recipe-yield log yet. Stub.
    return []


@anomaly_rule(
    name="kitchen.ingredient_oos",
    bucket="on_write", severity="alert",
    surfaces=["kds.station", "home", "orders.by_store", "partner.anomalies"],
    audience_roles=["partner", "gm", "km", "expo", "foh_manager"],
    action_text="86 affected menu items in Toast immediately. Place emergency order if same-day-recoverable.",
)
def kitchen_ingredient_oos(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: OOS flag set on the KDS inventory pane. No
    # inventory schema yet. Stub.
    return []


@anomaly_rule(
    name="kitchen.equipment_down",
    bucket="every_15m", severity="alert",
    surfaces=["kds.station", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm", "km", "corporate_chef"],
    action_text="Call repair. Adjust prep plan to route around. Update the log when fixed (auto-clears).",
)
def kitchen_equipment_down(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: manager_log tagged 'equipment_down' with status
    # 'open'. ManagerMessage exists but no tag/status fields yet. Stub.
    return []


# ============================================================
# 3.8 Customer (3 rules) — all Google-Reviews-dependent stubs
# ============================================================

@anomaly_rule(
    name="customer.review_unanswered",
    bucket="hourly", severity="warn",
    surfaces=["home", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "corporate", "gm", "foh_manager"],
    action_text="Respond on Google Business Profile. AI draft available at /partner/reviews/draft.",
)
def customer_review_unanswered(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: Google Business Profile API not wired. Stub.
    return []


@anomaly_rule(
    name="customer.review_negative",
    bucket="hourly", severity="alert",
    surfaces=["home", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm", "foh_manager"],
    action_text="Read, then respond same-day. Trace the review to a ticket / visit + surface to staff.",
)
def customer_review_negative(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: Google Business Profile API not wired. Stub.
    return []


@anomaly_rule(
    name="customer.complaint_trend",
    bucket="on_write", severity="warn",
    surfaces=["home", "partner.anomalies", "morning_brief"],
    audience_roles=["partner", "gm", "km", "foh_manager"],
    action_text="Identify the pattern (food / service / item). Address mid-shift if possible; debrief at shift-end.",
)
def customer_complaint_trend(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: > 2 complaints on the same shift (manager_log
    # tagged 'complaint' OR Toast guest_complaint tag). Neither
    # source plumbed yet. Stub.
    return []


# ============================================================
# 3.9 System / ops (3 rules)
# ============================================================

_RENDER_API = "https://api.render.com/v1"
_CENAS_EZLIVE_SERVICE_ID = "srv-d7ue6ivlk1mc73aaka20"


def _render_api_key() -> str | None:
    import os
    from pathlib import Path
    val = os.environ.get("RENDER_API_KEY", "").strip()
    if val:
        return val
    f = Path.home() / ".openclaw" / ".secrets" / "render_api_key.txt"
    if f.exists():
        return f.read_text().strip()
    return None


@anomaly_rule(
    name="system.deploy_failed",
    bucket="every_15m", severity="alert",
    surfaces=["home", "partner.anomalies"],
    audience_roles=["partner"],
    action_text="Surface the failed deploy URL and the failing commit; aick investigates.",
)
def system_deploy_failed(db: Session) -> list[SignalDraft]:
    """Render API: last deploy status = 'failed' or 'build_failed' or
    'update_failed' within the last 30 min."""
    import json
    import urllib.request
    import urllib.error
    api = _render_api_key()
    if not api:
        return []
    url = (f"{_RENDER_API}/services/{_CENAS_EZLIVE_SERVICE_ID}"
           "/deploys?limit=5")
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return []
    out: list[SignalDraft] = []
    cutoff = datetime.utcnow() - timedelta(minutes=30)
    failed_statuses = {"build_failed", "update_failed", "canceled",
                       "deactivated", "pre_deploy_failed"}
    for item in data[:5]:
        d = item.get("deploy") if isinstance(item, dict) else None
        if not d:
            continue
        status = d.get("status")
        if status not in failed_statuses:
            continue
        finished_str = d.get("finishedAt") or d.get("createdAt") or ""
        try:
            finished = datetime.fromisoformat(
                finished_str.rstrip("Z")[:19])
        except ValueError:
            continue
        if finished < cutoff:
            continue
        commit_id = (d.get("commit") or {}).get("id", "")[:7] or "?"
        out.append(SignalDraft(
            rule_name="system.deploy_failed",
            severity="alert",
            store_id=None,
            subject_id=d.get("id"),
            subject_label=f"Render deploy {commit_id} ({status})",
            payload={
                "deploy_id": d.get("id"),
                "commit": commit_id,
                "status": status,
                "finished_at_iso": finished_str,
            },
            action_text=("Render deploy failed — check the failing commit + "
                         "build logs."),
            surfaces=["home", "partner.anomalies"],
            audience_roles=["partner"],
        ))
        # Only fire on the most recent failure; older ones get
        # reported but de-duped by subject_id (the deploy id).
        break
    return out


@anomaly_rule(
    name="system.backup_missing",
    bucket="daily", severity="alert",
    surfaces=["partner.anomalies", "morning_brief"],
    audience_roles=["partner"],
    action_text="Trigger manual backup. Investigate cron schedule on AiCk or Render scheduled job.",
)
def system_backup_missing(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: no automated SQLite backup story yet (open Phase 2
    # liability flagged 2026-05-13 in the Block 1 SQLite-vs-Postgres
    # call). Until a backup job exists, this rule has nothing to check
    # — but it's registered so the morning brief notes "no backup
    # mechanism present" in the calibration footer. Stub.
    return []


@anomaly_rule(
    name="system.api_key_expiring",
    bucket="daily", severity="warn",
    surfaces=["partner.anomalies", "morning_brief"],
    audience_roles=["partner"],
    action_text="Rotate the key. Update Render env var via aick's Render API path.",
)
def system_api_key_expiring(db: Session) -> list[SignalDraft]:
    # TODO Phase 2: needs an api_key_metadata table with expiration
    # info per env var. Today nothing tracks token expiry across
    # Toast, Google Routes, etc. Stub.
    return []
