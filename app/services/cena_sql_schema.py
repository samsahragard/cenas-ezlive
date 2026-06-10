"""C.E.N.A. Level 3 - curated schema context + table/column allowlist.

Owner: Subagent A. Contract: docs/cena_level3_contracts.md (sections 1, 4, 5).

Public API (frozen):
    get_schema_context() -> str
    get_allowlist() -> dict[str, frozenset[str]]
Additive helpers (used by cena_sql_validator, safe for others):
    get_excluded_columns() -> dict[str, frozenset[str]]
    clear_caches() -> None
    SCHEMA_ALIASES, ANALYTICS_TABLES

Design:
- The allowlist is built by introspecting the real source SQLite files READ-ONLY
  (env-overridable paths, contract section 1). Introspection is cached. If a source
  file is missing/unreadable, we fall back per-table to a static column snapshot
  (captured live 2026-06-09) so import and tests never require real DBs.
- Curation is an explicit INCLUDE list per source. Section 5 exclusions are
  enforced, and further junk/empty/misleading tables are excluded with reasons
  documented inline below.
- Analytics table columns are the FROZEN section-4 stub (never introspected);
  cena_sql_analytics.SCHEMA_DOC is imported at runtime ONLY to enrich the schema
  context text, falling back silently to the section-4 summary.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path

SCHEMA_ALIASES: tuple[str, ...] = ("appdb", "toast", "toastdm", "ordersdc", "driverdc")

# alias -> (env var, default path)  [contract section 1]
_SOURCE_DEFAULTS: dict[str, tuple[str, str]] = {
    "appdb": ("CENA_L3_SRC_APPDB", r"C:\Users\sam\cenas-ezlive\dev_local.db"),
    "toast": ("CENA_L3_SRC_TOAST", r"C:\Users\sam\cena-perfdb\perf.sqlite"),
    "toastdm": ("CENA_L3_SRC_TOASTDM", r"C:\Users\sam\cena-perfdb\datamart\datamart.sqlite"),
    "ordersdc": ("CENA_L3_SRC_ORDERSDC", r"C:\Users\sam\cena-driverdc\_live\ordersdc.sqlite"),
    "driverdc": ("CENA_L3_SRC_DRIVERDC", r"C:\Users\sam\cena-driverdc\_live\driverdc.sqlite"),
}


def _source_paths() -> dict[str, str]:
    return {a: os.environ.get(env) or default for a, (env, default) in _SOURCE_DEFAULTS.items()}


# ---------------------------------------------------------------------------
# Analytics schema (contract section 4, FROZEN). Hardcoded stub - the allowlist
# always uses these columns; Subagent C builds the same shapes.
# ---------------------------------------------------------------------------
ANALYTICS_TABLES: dict[str, tuple[str, ...]] = {
    "daily_sales_summary": (
        "store_key", "business_date", "net_sales", "gross_sales", "order_count",
        "check_count", "avg_check", "covers", "instore_net", "daypart_breakfast_net",
        "daypart_lunch_net", "daypart_dinner_net", "built_at"),
    "daily_labor_summary": (
        "store_key", "business_date", "total_hours", "reg_hours", "ot_hours",
        "labor_cost", "net_sales", "labor_pct", "splh", "employee_count", "built_at"),
    "weekly_rollups": (
        "store_key", "iso_week", "week_start", "net_sales", "order_count",
        "labor_cost", "labor_pct", "splh", "total_hours", "wow_net_sales_delta",
        "wow_net_sales_pct", "wow_labor_pct_delta", "built_at"),
    "item_sales_summary": (
        "store_key", "business_date", "item_name", "category", "qty", "net_amount",
        "built_at"),
    "daypart_comparison": (
        "store_key", "business_date", "daypart", "net_sales", "order_count", "built_at"),
    "same_day_lastweek": (  # VIEW
        "store_key", "business_date", "day_of_week", "net_sales",
        "prev_week_net_sales", "delta", "pct_change"),
    "anomaly_flags": (
        "store_key", "business_date", "metric", "value", "baseline_mean",
        "baseline_std", "z_score", "direction", "built_at"),
}

# ---------------------------------------------------------------------------
# Curated table INCLUDE list. Anything not listed is excluded.
#
# Section-5 exclusions enforced by omission: appdb users / permission_denial /
# user_audit_log / legal_* / sam_chat_* / developer_chat* / dev_chat_* / docck_* /
# cena_action_logs / cena_usage_logs / cena_wake_decisions / access_request /
# paycheck / processing_* / failure_snapshots / ribbon_* / rule_overrides /
# interview_candidates / sample_* / in_house_catering_quotes; toast.perf_internal
# (ISOLATED sales lane); toastdm.dm_internal_sales (same lane); ordersdc.dm_ingest_ledger.
#
# Additional CURATED exclusions (correctness calls, verified against live data
# 2026-06-09 - exposing these would mislead the reasoner):
# - appdb: every other operational table is EMPTY in the source copy
#   (ambient_*, brief_feedback, cancellation, daily_log_entry_image,
#   driver_assignment_jobs, driver_location, driver_logs, driver_notification,
#   driver_score, driver_shift, ezcater_order_details, fresh_food_order*,
#   kitchen_prep_entry, manager_* [all 0 rows], morning_briefs, order_items,
#   prep_breakdowns, produce_price_snapshot, sales_insights, scheduled_events,
#   signal_acks, signals, task_audit_log, tasks, vendor_recent_orders).
#   Empty tables invite confident "zero" answers when a richer source exists:
#   order_items -> ordersdc.dm_order_item; driver_score -> driverdc.dm_driver_score;
#   manager_attendance_* -> toast.time_entry.
# - toast.employee / toast.employee_store: EMPTY (0 rows); employee names live in
#   toastdm.dm_profile. toast.meta / toast.sync_run: sync plumbing, no analytics value.
# - toastdm.dm_rank: rank_json embeds per-NAMED-employee effective_hourly dollar
#   leaderboards (verified live) = individual pay leak. Rank data is available
#   safely via toast.rank_snapshot (rank/pct_rank, value_metric stripped).
# - toastdm.dm_meta, ordersdc.dm_order_meta, driverdc.dm_driver_meta: refresh
#   plumbing tables.
# - driverdc.dm_attendance: empty BY DESIGN (driver_shift is not in the frozen
#   driverdc-v3 export contract).
# ---------------------------------------------------------------------------
_CURATED_TABLES: dict[str, tuple[str, ...]] = {
    "appdb": ("orders", "drivers", "delivery_request", "ezcater_known_driver",
              "kitchen_prep_item", "recipes"),
    "toast": ("time_entry", "perf_period", "rank_snapshot"),
    "toastdm": ("dm_time_entry", "dm_schedule", "dm_profile", "dm_perf_period",
                "dm_attendance", "dm_employee_store"),
    "ordersdc": ("dm_order", "dm_order_item", "dm_order_timing", "dm_order_driver",
                 "dm_customer"),
    "driverdc": ("dm_driver", "dm_delivery", "dm_pay", "dm_driver_score"),
}

# Explicit per-table column exclusions (contract section 5 + curated additions).
_EXPLICIT_EXCLUDED_COLUMNS: dict[str, frozenset[str]] = {
    # section 5: PII / customer contact
    "appdb.orders": frozenset({
        "client", "upon_delivery_ask_for", "customer_phone", "delivery_address",
        "delivery_instructions", "ezcater_driver_lat", "ezcater_driver_lng",
        "pay_notes", "source_filename"}),
    # section 5: driver PII / auth
    "appdb.drivers": frozenset({
        "email", "phone", "address", "password_hash", "passcode_hash",
        "last_known_lat", "last_known_lng", "failed_attempts", "lockout_until",
        "session_version", "photo_url"}),
    # section 5: individual pay
    "toast.time_entry": frozenset({"hourly_rate", "tips", "tips_declared"}),
    # section 5 (base_pay/tips/tip_pct) + curated: service_json/attendance_json are
    # attribution plumbing blobs carrying raw Toast employee GUIDs, no analytic value.
    "toast.perf_period": frozenset({
        "base_pay", "tips", "tip_pct", "service_json", "attendance_json"}),
    # curated: value_metric carries effective_hourly / tips_per_hour / tip_percent
    # VALUES = individual pay rates (verified live). "hours/rank ok" - rank stays.
    "toast.rank_snapshot": frozenset({"value_metric"}),
    # section 5: individual pay
    "toastdm.dm_time_entry": frozenset({"base_pay", "tips", "tips_declared"}),
    # section 5 + curated JSON plumbing (raw Toast GUIDs)
    "toastdm.dm_perf_period": frozenset({
        "base_pay", "tips", "tip_pct", "service_json", "attribution_json"}),
}

# Generic column hygiene (section 5 closing rule): any email/phone/address column
# anywhere; any *_hash except customer_hash; plus password/passcode defensively.
_GENERIC_BAD_SUBSTRINGS = ("email", "phone", "address", "password", "passcode")


def _column_is_excluded(qualified: str, col: str) -> bool:
    lc = col.lower()
    if lc in _EXPLICIT_EXCLUDED_COLUMNS.get(qualified, frozenset()):
        return True
    if any(s in lc for s in _GENERIC_BAD_SUBSTRINGS):
        return True
    if lc.endswith("_hash") and lc != "customer_hash":
        return True
    return False


# ---------------------------------------------------------------------------
# Static fallback: full RAW column lists captured from the live sources on
# 2026-06-09 (pre-exclusion; the same filter runs on both live and static paths).
# Used per-table whenever a source file is missing or a curated table is absent.
# ---------------------------------------------------------------------------
_STATIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "appdb.orders": (
        "id", "created_at", "updated_at", "source_filename", "external_order_id",
        "external_delivery_id", "client", "upon_delivery_ask_for", "customer_phone",
        "delivery_address", "delivery_instructions", "headcount", "reported_store",
        "reported_store_id", "origin_store_id", "delivery_date", "deliver_at",
        "delivery_window", "setup_required", "status", "needs_review", "warning_count",
        "flags", "total_amount", "tracking_status", "ezcater_driver_name",
        "pickup_kitchen", "pickup_miles", "food_total", "tip_amount", "delivery_fee",
        "caterer_total_due", "delivery_result", "delivery_start_time",
        "delivery_complete_time", "delivery_tracking_id", "ezcater_status_key",
        "ezcater_driver_lat", "ezcater_driver_lng", "ezcater_status_updated_at",
        "kitchen_ready_time", "driver_departure_time", "assigned_driver",
        "route_group_id", "route_stop_index", "delivery_window_start",
        "delivery_window_end", "customer_rating", "setup_photo_url",
        "setup_photo_uploaded_at", "potential_payout", "paid_payout", "paycheck_id",
        "assigned_driver_id", "approved_by_user_id", "approved_at", "pickup_actual_at",
        "en_route_at", "delivered_actual_at", "pay_verified_miles", "pay_driven_miles",
        "pay_bonus_tracked", "pay_five_star", "pay_notes", "pay_verified_at",
        "pay_verified_by"),
    "appdb.drivers": (
        "id", "name", "location", "created_at", "email", "phone", "address",
        "password_hash", "active", "failed_attempts", "lockout_until", "passcode_hash",
        "first_login_done", "session_version", "status", "terminated_at",
        "termination_reason", "joined_at", "lifetime_delivery_count", "current_score",
        "current_tier", "home_store_id", "last_known_lat", "last_known_lng",
        "last_location_at", "photo_url", "battery_opt_ignored",
        "battery_opt_checked_at"),
    "appdb.delivery_request": (
        "id", "delivery_id", "driver_id", "requested_at", "status", "decided_at",
        "decided_by_user_id"),
    "appdb.ezcater_known_driver": ("id", "created_at", "name", "phone_e164", "ck_prefix"),
    "appdb.kitchen_prep_item": (
        "id", "name", "category", "kind", "recipe_id", "sort_order", "active",
        "store_scope"),
    "appdb.recipes": (
        "id", "created_at", "updated_at", "code", "category", "name", "prep_time",
        "shelf_life", "spanish_instructions", "english_instructions",
        "ingredients_json", "batch_sizes_json", "notes"),
    "toast.time_entry": (
        "id", "cena_employee_id", "toast_employee_id", "store_key", "business_date",
        "clock_in", "clock_out", "reg_hours", "ot_hours", "hourly_rate", "tips",
        "tips_declared", "needs_review", "review_reason", "source"),
    "toast.perf_period": (
        "id", "cena_employee_id", "toast_employee_id", "store_key", "period",
        "period_start", "period_end", "reg_hours", "ot_hours", "total_hours",
        "base_pay", "tips", "tip_pct", "service_json", "attendance_json",
        "rank_in_store", "rank_metric", "computed_at"),
    "toast.rank_snapshot": (
        "id", "snapshot_date", "period", "metric", "cohort_key", "cohort_size",
        "cena_employee_id", "store_key", "rank", "pct_rank", "value_metric",
        "qualified", "computed_at"),
    "toastdm.dm_time_entry": (
        "cena_employee_id", "store_key", "business_date", "clock_in", "clock_out",
        "reg_hours", "ot_hours", "total_hours", "base_pay", "tips", "tips_declared",
        "needs_review", "review_reason", "generated_at"),
    "toastdm.dm_schedule": (
        "cena_employee_id", "shift_uid", "store_key", "position_name", "start_at",
        "end_at", "status", "generated_at"),
    "toastdm.dm_profile": (
        "cena_employee_id", "full_name", "active", "primary_store_key",
        "positions_json", "hire_date", "source", "generated_at"),
    "toastdm.dm_perf_period": (
        "cena_employee_id", "store_key", "period", "period_start", "period_end",
        "total_hours", "reg_hours", "ot_hours", "base_pay", "tips", "service_json",
        "attribution_json", "computed_at", "generated_at"),
    "toastdm.dm_attendance": (
        "cena_employee_id", "period", "period_start", "period_end", "shifts_total",
        "needs_review_count", "last_review_reason", "last_review_date", "generated_at"),
    "toastdm.dm_employee_store": ("cena_employee_id", "store_key", "generated_at"),
    "ordersdc.dm_order": (
        "external_order_id", "external_delivery_id", "store_key", "delivery_date",
        "window_start", "window_end", "status", "headcount", "tracking_status",
        "ezcater_total", "food_total", "delivery_fee", "tip_amount",
        "caterer_total_due", "discounts", "taxes", "fees", "commissions",
        "service_fees", "processing_fees", "toast_app_total", "gross_minus_payout",
        "customer_hash", "customer_salt_version", "generated_at"),
    "ordersdc.dm_order_item": (
        "external_order_id", "item_key", "name", "category", "menu_group", "qty",
        "modifiers_json", "unit_price", "line_total", "generated_at"),
    "ordersdc.dm_order_timing": (
        "external_order_id", "delivery_result", "delivery_start", "delivery_complete",
        "delivered_actual_at", "on_time", "generated_at"),
    "ordersdc.dm_order_driver": (
        "external_order_id", "driver_id", "link_method", "ezcater_driver_name",
        "generated_at"),
    "ordersdc.dm_customer": (
        "customer_hash", "salt_version", "first_seen", "last_seen", "order_count",
        "store_keys", "lifetime_value", "generated_at"),
    "driverdc.dm_driver": (
        "driver_id", "name", "active", "status", "home_store_key", "current_tier",
        "current_score", "joined_at", "lifetime_delivery_count", "generated_at"),
    "driverdc.dm_delivery": (
        "driver_id", "external_order_id", "business_date", "status", "on_time",
        "tracking_ok", "proof_photo_present", "parking_proof_present", "gps_miles",
        "tracking_status", "pickup_miles", "pay_verified_miles", "pay_driven_miles",
        "pay_bonus_tracked", "pay_five_star", "driver_payout", "verified_miles_payout",
        "tracked_bonus", "five_star_bonus", "parking_cost", "generated_at"),
    "driverdc.dm_pay": (
        "driver_id", "period", "period_start", "period_end", "pay_in_total",
        "gross_out", "net_out", "tracked_bonus", "five_star_bonus", "miles_payout",
        "parking_reimb", "total_driver_pay", "generated_at"),
    "driverdc.dm_driver_score": (
        "driver_id", "computed_at", "window_start", "window_end", "score", "tier",
        "tracking_pts", "on_time_pts", "cancellation_pts", "photo_pts",
        "response_pts", "star_pts", "generated_at"),
}

# ---------------------------------------------------------------------------
# Introspection (read-only, cached) with graceful per-table static fallback.
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_cache: dict[str, object] = {}


def clear_caches() -> None:
    """Reset cached introspection/allowlist/context (tests + after env changes)."""
    with _lock:
        _cache.clear()


def _introspect_alias(alias: str, path: str) -> dict[str, tuple[str, ...]] | None:
    """Return {table: raw column tuple} for the curated tables of one source DB,
    or None when the file is missing/unreadable (caller falls back to statics)."""
    if not path or not os.path.isfile(path):
        return None
    uri = "file:" + Path(path).resolve().as_posix() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    try:
        out: dict[str, tuple[str, ...]] = {}
        for tname in _CURATED_TABLES[alias]:
            if not re.fullmatch(r"[A-Za-z0-9_]+", tname):  # defense in depth
                continue
            try:
                rows = con.execute(f'PRAGMA table_info("{tname}")').fetchall()
            except sqlite3.Error:
                rows = []
            if rows:
                out[tname] = tuple(r[1] for r in rows)
        return out
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _build() -> tuple[dict[str, tuple[str, ...]], dict[str, frozenset[str]]]:
    """Build (ordered allowed columns per table, excluded columns per table)."""
    paths = _source_paths()
    ordered: dict[str, tuple[str, ...]] = {}
    excluded: dict[str, frozenset[str]] = {}
    for alias in SCHEMA_ALIASES:
        live = _introspect_alias(alias, paths[alias])
        for tname in _CURATED_TABLES[alias]:
            qualified = f"{alias}.{tname}"
            raw = None
            if live is not None:
                raw = live.get(tname)
            if not raw:  # source missing OR table absent in live DB -> static
                raw = _STATIC_COLUMNS[qualified]
            keep, drop = [], []
            for c in raw:
                (drop if _column_is_excluded(qualified, c) else keep).append(c.lower())
            ordered[qualified] = tuple(keep)
            if drop:
                excluded[qualified] = frozenset(drop)
    for tname, cols in ANALYTICS_TABLES.items():
        ordered[tname] = tuple(cols)
    return ordered, excluded


def _built() -> tuple[dict[str, tuple[str, ...]], dict[str, frozenset[str]]]:
    with _lock:
        if "ordered" not in _cache:
            _cache["ordered"], _cache["excluded"] = _build()
        return _cache["ordered"], _cache["excluded"]  # type: ignore[return-value]


def get_allowlist() -> dict[str, frozenset[str]]:
    """Qualified raw tables ('toast.time_entry') and unqualified analytics tables
    ('daily_sales_summary') -> frozenset of allowed column names (lowercase)."""
    with _lock:
        cached = _cache.get("allowlist")
    if cached is not None:
        return cached  # type: ignore[return-value]
    ordered, _ = _built()
    allow = {t: frozenset(cols) for t, cols in ordered.items()}
    with _lock:
        _cache["allowlist"] = allow
    return allow


def get_excluded_columns() -> dict[str, frozenset[str]]:
    """Tables that CARRY exclusions -> the excluded column names. The validator
    uses this to (a) block SELECT * on those tables and (b) say 'excluded by
    policy' instead of 'no such column' in rejection reasons."""
    _, excluded = _built()
    return excluded


# ---------------------------------------------------------------------------
# Schema context text
# ---------------------------------------------------------------------------
_ANALYTICS_DOC_BUDGET = 4500  # cap imported SCHEMA_DOC so total stays < 16k chars

# Short, load-bearing notes per raw table (verified against live data 2026-06-09).
_TABLE_NOTES: dict[str, str] = {
    "ordersdc.dm_order":
        "846 rows, delivery_date 2026-02-10..2026-08-01 INCLUDES FUTURE bookings. "
        "caterer_total_due=NET to business (USE FOR 'sales'); ezcater_total=customer "
        "GROSS incl fees/taxes; food_total=food only. store_key is RAW store_1..4 "
        "(see gotchas). customer_hash=opaque pseudonym. window_start/window_end local ISO.",
    "ordersdc.dm_order_item":
        "1092 rows. line_total=net line amount. category/menu_group currently NULL - "
        "group/filter by name.",
    "ordersdc.dm_order_timing":
        "on_time currently NULL - derive lateness from delivery_result text "
        "('On time','One to fifteen minutes late','More than sixty minutes late',...).",
    "ordersdc.dm_order_driver":
        "link_method in (fk,fuzzy_name,unlinked). driver_id -> driverdc.dm_driver.",
    "ordersdc.dm_customer":
        "609 pseudonymous customers. store_keys=JSON array of raw store_1..4. "
        "lifetime_value=net dollars.",
    "appdb.orders":
        "ONLY ~8 rows (recent app copy, 2026-05-25..29). Use ordersdc.dm_order for "
        "order history; this table adds live ops fields (tracking, driver pay-verify, "
        "route grouping). Timestamps UTC; deliver_at is a clock string like '12:30 PM'.",
    "appdb.drivers":
        "13 in-house drivers. location in (copperfield,tomball). current_score/"
        "current_tier mirror driverdc scoring.",
    "appdb.delivery_request": "driver bid/assignment requests (2 rows).",
    "appdb.ezcater_known_driver": "ezCater marketplace driver name registry (37 rows).",
    "appdb.kitchen_prep_item": "prep item catalog (44 rows), store_scope scoping.",
    "appdb.recipes": "recipe book (10 rows): code, category, prep_time, instructions.",
    "toast.time_entry":
        "CANONICAL labor, 1784 shifts, business_date 2026-05-11..today. clock_in/"
        "clock_out UTC ISO. Pay rates/tips are NOT exposed - aggregated labor cost "
        "lives in daily_labor_summary.",
    "toast.perf_period":
        "period SNAPSHOT (today/week/month/last30), NOT history. hours + rank_in_store "
        "ok; pay columns excluded.",
    "toast.rank_snapshot":
        "rank history by metric (effective_hourly/combined/tip_percent/tips_per_hour); "
        "rank/pct_rank only, metric VALUES excluded (individual pay).",
    "toastdm.dm_time_entry":
        "labor cross-check, 2293 rows back to 2026-05-04 (earlier than toast.time_entry).",
    "toastdm.dm_schedule":
        "1865 shifts 2026-03-01..future, local ISO start_at/end_at; status always "
        "'assigned' today.",
    "toastdm.dm_profile":
        "88 employees: full_name, active, primary_store_key, positions_json, hire_date "
        "(often NULL). THE employee-name lookup (toast.employee is empty).",
    "toastdm.dm_perf_period": "period SNAPSHOT like toast.perf_period (hours only).",
    "toastdm.dm_attendance": "period SNAPSHOT attendance counters (shifts_total, reviews).",
    "toastdm.dm_employee_store": "employee<->store mapping (97 rows).",
    "driverdc.dm_driver":
        "48 driver profiles. home_store_key uses slugs 'uno'=copperfield, "
        "'dos'=tomball (often NULL).",
    "driverdc.dm_delivery":
        "per-delivery economics (20 rows, new lane, 2026-05-19+). driver_payout & "
        "bonus fields are management-visible delivery costs.",
    "driverdc.dm_pay": "driver pay rollup SNAPSHOT; period only 'last30' today.",
    "driverdc.dm_driver_score": "score/tier history (859 rows) with point components.",
}

_SECTION_HEADERS: dict[str, str] = {
    "ordersdc": "ordersdc - ezCater order history mart (THE rich order/sales source)",
    "appdb": "appdb - live app DB copy (ops state; tiny order sample)",
    "toast": "toast - canonical Toast labor lane (perf.sqlite)",
    "toastdm": "toastdm - Toast datamart (labor cross-check, profiles, schedule)",
    "driverdc": "driverdc - driver profile/economics mart",
}

_FALLBACK_ANALYTICS_DOC = """- daily_sales_summary(store_key, business_date, net_sales, gross_sales, order_count, check_count, avg_check, covers, instore_net, daypart_breakfast_net, daypart_lunch_net, daypart_dinner_net, built_at)
  Daily CATERING rollup from ordersdc.dm_order. net_sales=SUM(caterer_total_due) (NET to business - use for 'sales'); gross_sales=SUM(ezcater_total) (customer gross incl fees/taxes); check_count=order_count (1 check per catering order); avg_check=net_sales/order_count; covers=SUM(headcount); instore_net ALWAYS NULL (in-store sales have no daily source). Dayparts by local window_start: breakfast<10:30, lunch 10:30-14:59, dinner>=15:00 (NULL->lunch).
- daily_labor_summary(store_key, business_date, total_hours, reg_hours, ot_hours, labor_cost, net_sales, labor_pct, splh, employee_count, built_at)
  From toast.time_entry. labor_cost=SUM(reg_hours*rate + 1.5*ot_hours*rate) PRE-AGGREGATED (individual rates never exposed). net_sales joined from daily_sales_summary = CATERING net, so labor_pct=labor_cost/net_sales*100 is labor-vs-CATERING-net (whole-store labor vs catering-only sales - always describe it that way; NULL when net_sales 0/NULL). splh=net_sales/total_hours.
- weekly_rollups(store_key, iso_week, week_start, net_sales, order_count, labor_cost, labor_pct, splh, total_hours, wow_net_sales_delta, wow_net_sales_pct, wow_labor_pct_delta, built_at)
  iso_week like '2026-W23', weeks start Monday; wow_* = week-over-week deltas.
- item_sales_summary(store_key, business_date, item_name, category, qty, net_amount, built_at)
  From ordersdc.dm_order_item joined dm_order (line_total as net_amount). category often NULL.
- daypart_comparison(store_key, business_date, daypart, net_sales, order_count, built_at)
  Long form of the daypart splits (daypart in breakfast/lunch/dinner).
- same_day_lastweek VIEW (store_key, business_date, day_of_week, net_sales, prev_week_net_sales, delta, pct_change)
  Same weekday, prior week anchor.
- anomaly_flags(store_key, business_date, metric, value, baseline_mean, baseline_std, z_score, direction, built_at)
  metric in ('net_sales','labor_pct','avg_check'); baseline = trailing 8 SAME-WEEKDAY values (min 4, std>0); flagged when |z|>2; direction 'high'|'low'."""


def _analytics_doc() -> str:
    """Prefer cena_sql_analytics.SCHEMA_DOC at runtime; silently fall back to the
    frozen section-4 summary. Truncated to a budget so context stays token-lean."""
    doc = None
    try:  # pragma: no cover - exercised via sys.modules injection in tests
        import importlib

        mod = importlib.import_module("app.services.cena_sql_analytics")
        doc = getattr(mod, "SCHEMA_DOC", None)
    except Exception:
        doc = None
    if not isinstance(doc, str) or not doc.strip():
        doc = _FALLBACK_ANALYTICS_DOC
    doc = doc.strip()
    if len(doc) > _ANALYTICS_DOC_BUDGET:
        doc = doc[:_ANALYTICS_DOC_BUDGET] + "\n  ...[truncated]"
    return doc


def _render_context(ordered: dict[str, tuple[str, ...]]) -> str:
    parts: list[str] = []
    parts.append(
        "# C.E.N.A. SQL schema (SQLite, READ-ONLY; one SELECT per query, WITH...SELECT ok)\n"
        "Analytics tables are UNQUALIFIED: daily_sales_summary, daily_labor_summary, "
        "weekly_rollups, item_sales_summary, daypart_comparison, same_day_lastweek, "
        "anomaly_flags.\n"
        "Raw tables MUST be schema-qualified: appdb.*, toast.*, toastdm.*, ordersdc.*, "
        "driverdc.*.\n"
        "Conventions: canonical store_key values are 'copperfield' and 'tomball' "
        "(toast/toastdm/analytics). business_date / delivery_date are local-business ISO "
        "'YYYY-MM-DD' strings (America/Chicago). appdb timestamps and toast/toastdm "
        "clock_in/clock_out are UTC; toastdm.dm_schedule start_at/end_at are local ISO."
    )
    parts.append(
        "## CRITICAL GOTCHAS\n"
        "- 'Sales' = catering NET: ordersdc.dm_order.caterer_total_due (what Cenas "
        "receives). ezcater_total = customer-paid GROSS incl fees/taxes. food_total = "
        "food only. Never present gross as sales.\n"
        "- dm_order.delivery_date extends into the FUTURE (orders booked ahead). Any "
        "history/'how did we do' question MUST filter "
        "delivery_date <= date('now','localtime').\n"
        "- appdb.orders has only ~8 rows (dev copy). The RICH order history (846 "
        "orders, 6 months) is ordersdc.dm_order - default to it for sales/order "
        "questions.\n"
        "- Store keys differ by lane: toast/toastdm/analytics use copperfield/tomball. "
        "ordersdc uses RAW store_1..store_4: store_1+store_3 -> copperfield, "
        "store_2+store_4 -> tomball (store_3/4 are ghost storefronts collapsed onto the "
        "physical kitchens). driverdc.dm_driver.home_store_key uses 'uno'=copperfield, "
        "'dos'=tomball.\n"
        "- Labor: toast.time_entry is CANONICAL (2026-05-11 onward); "
        "toastdm.dm_time_entry reaches back to 2026-05-04 (cross-check source).\n"
        "- labor_pct in analytics = labor cost vs CATERING net sales (daily in-store "
        "sales are not available); describe it as labor-vs-catering-net.\n"
        "- perf tables (toast.perf_period, toastdm.dm_perf_period, dm_attendance, "
        "driverdc.dm_pay) are period SNAPSHOTS ('today','week','month','last30'), NOT "
        "history - do not sum periods over time.\n"
        "- Individual pay (rates, tips, base pay) is NOT queryable anywhere; labor "
        "cost exists only pre-aggregated in daily_labor_summary.\n"
        "- ordersdc.dm_order_item.category/menu_group and dm_order_timing.on_time are "
        "currently NULL - use item name and delivery_result text instead."
    )
    parts.append(
        "## JOIN PATHS\n"
        "- ordersdc: dm_order.external_order_id = dm_order_item.external_order_id = "
        "dm_order_timing.external_order_id = dm_order_driver.external_order_id.\n"
        "- dm_order_driver.driver_id -> driverdc.dm_driver.driver_id (also dm_delivery/"
        "dm_pay/dm_driver_score.driver_id).\n"
        "- appdb.orders.external_order_id ~ ordersdc.dm_order.external_order_id (same "
        "ezCater ids).\n"
        "- toast.time_entry.cena_employee_id -> toastdm.dm_profile.cena_employee_id "
        "(names) and toastdm.* tables.\n"
        "- Analytics tables join each other on (store_key, business_date)."
    )
    parts.append("## ANALYTICS TABLES (cena_analytics.db, unqualified; every table has "
                 "built_at TEXT ISO-UTC)\n" + _analytics_doc())
    for alias in ("ordersdc", "appdb", "toast", "toastdm", "driverdc"):
        lines = [f"## {_SECTION_HEADERS[alias]}"]
        for tname in _CURATED_TABLES[alias]:
            qualified = f"{alias}.{tname}"
            note = _TABLE_NOTES.get(qualified, "")
            cols = ", ".join(ordered[qualified])
            lines.append(f"- {qualified}: {note}\n  cols: {cols}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def get_schema_context() -> str:
    """Curated, token-efficient schema description of ALLOWLISTED tables only."""
    with _lock:
        cached = _cache.get("context")
    if cached is not None:
        return cached  # type: ignore[return-value]
    ordered, _ = _built()
    text = _render_context(ordered)
    with _lock:
        _cache["context"] = text
    return text
