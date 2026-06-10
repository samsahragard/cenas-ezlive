"""Tests for app/services/cena_sql_schema.py (Subagent A).

Hermetic: source-path env vars are pointed at nonexistent files so the module
exercises its static fallback; the introspection path is tested against a
synthetic SQLite file in tmp_path. An optional real-data smoke test is guarded
by skipif on the default source path.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import types

import pytest

from app.services import cena_sql_schema as schema_mod

_ENV_VARS = {
    "appdb": "CENA_L3_SRC_APPDB",
    "toast": "CENA_L3_SRC_TOAST",
    "toastdm": "CENA_L3_SRC_TOASTDM",
    "ordersdc": "CENA_L3_SRC_ORDERSDC",
    "driverdc": "CENA_L3_SRC_DRIVERDC",
}


@pytest.fixture(autouse=True)
def _isolated_sources(monkeypatch, tmp_path):
    """Point every source at a missing file (hermetic static-fallback mode) and
    block the optional cena_sql_analytics enrichment import so tests stay
    deterministic whether or not the sibling module exists yet (None in
    sys.modules makes `import` raise ImportError -> our silent fallback).
    Enrichment tests override the sys.modules entry themselves."""
    for alias, env in _ENV_VARS.items():
        monkeypatch.setenv(env, str(tmp_path / f"missing_{alias}.sqlite"))
    monkeypatch.setitem(sys.modules, "app.services.cena_sql_analytics", None)
    schema_mod.clear_caches()
    yield
    schema_mod.clear_caches()


# ---------------------------------------------------------------------------
# get_allowlist
# ---------------------------------------------------------------------------

def test_allowlist_shape_and_types():
    allow = schema_mod.get_allowlist()
    assert isinstance(allow, dict) and allow
    for table, cols in allow.items():
        assert isinstance(table, str)
        assert isinstance(cols, frozenset)
        assert cols, f"{table} has no columns"


def test_analytics_tables_match_frozen_section4():
    allow = schema_mod.get_allowlist()
    for tname, cols in schema_mod.ANALYTICS_TABLES.items():
        assert tname in allow
        assert allow[tname] == frozenset(cols)
    assert allow["daily_sales_summary"] >= {
        "store_key", "business_date", "net_sales", "gross_sales", "avg_check",
        "instore_net", "daypart_lunch_net", "built_at"}
    assert allow["daily_labor_summary"] >= {"labor_cost", "labor_pct", "splh"}
    assert "pct_change" in allow["same_day_lastweek"]
    assert "z_score" in allow["anomaly_flags"]


def test_key_operational_tables_present_with_correct_columns():
    allow = schema_mod.get_allowlist()
    assert allow["ordersdc.dm_order"] >= {
        "external_order_id", "store_key", "delivery_date", "status", "headcount",
        "ezcater_total", "food_total", "caterer_total_due", "customer_hash"}
    assert allow["ordersdc.dm_order_item"] >= {
        "external_order_id", "name", "qty", "line_total"}
    assert allow["ordersdc.dm_order_timing"] >= {"delivery_result", "on_time"}
    assert allow["toast.time_entry"] >= {
        "cena_employee_id", "store_key", "business_date", "clock_in", "clock_out",
        "reg_hours", "ot_hours"}
    assert allow["toastdm.dm_time_entry"] >= {
        "cena_employee_id", "business_date", "total_hours"}
    assert allow["toastdm.dm_profile"] >= {"cena_employee_id", "full_name"}
    assert allow["appdb.orders"] >= {
        "external_order_id", "delivery_date", "status", "caterer_total_due",
        "potential_payout", "paid_payout"}
    assert allow["appdb.drivers"] >= {"id", "name", "location", "current_tier"}
    assert allow["driverdc.dm_driver"] >= {"driver_id", "name", "home_store_key"}
    # driver delivery economics explicitly allowed (contract section 5)
    assert allow["driverdc.dm_delivery"] >= {"driver_payout", "tracked_bonus"}
    assert allow["driverdc.dm_pay"] >= {"total_driver_pay", "period"}


def test_section5_table_exclusions():
    allow = schema_mod.get_allowlist()
    for forbidden in (
        "appdb.users", "appdb.paycheck", "appdb.permission_denial",
        "appdb.user_audit_log", "appdb.in_house_catering_quotes",
        "appdb.interview_candidates", "appdb.cena_action_logs",
        "toast.perf_internal", "toastdm.dm_internal_sales",
        "ordersdc.dm_ingest_ledger",
    ):
        assert forbidden not in allow


def test_curated_table_exclusions():
    allow = schema_mod.get_allowlist()
    # pay leak via per-named-employee leaderboards (verified live 2026-06-09)
    assert "toastdm.dm_rank" not in allow
    # empty-by-design / plumbing
    assert "driverdc.dm_attendance" not in allow
    assert "toast.sync_run" not in allow
    assert "toast.meta" not in allow
    assert "ordersdc.dm_order_meta" not in allow
    # empty appdb tables superseded by richer marts
    assert "appdb.order_items" not in allow
    assert "appdb.driver_score" not in allow


def test_section5_column_exclusions():
    allow = schema_mod.get_allowlist()
    assert not allow["toast.time_entry"] & {"hourly_rate", "tips", "tips_declared"}
    assert not allow["toast.perf_period"] & {"base_pay", "tips", "tip_pct"}
    assert not allow["toastdm.dm_time_entry"] & {"base_pay", "tips", "tips_declared"}
    assert not allow["appdb.orders"] & {
        "client", "customer_phone", "delivery_address", "delivery_instructions",
        "upon_delivery_ask_for", "pay_notes"}
    assert not allow["appdb.drivers"] & {
        "email", "phone", "address", "password_hash", "passcode_hash",
        "last_known_lat", "last_known_lng"}
    # generic rules: phone anywhere; *_hash except customer_hash
    assert "phone_e164" not in allow["appdb.ezcater_known_driver"]
    assert "customer_hash" in allow["ordersdc.dm_order"]
    assert "customer_hash" in allow["ordersdc.dm_customer"]


def test_curated_column_exclusions():
    allow = schema_mod.get_allowlist()
    # rank ok, metric VALUES (pay rates) excluded
    assert "rank" in allow["toast.rank_snapshot"]
    assert "pct_rank" in allow["toast.rank_snapshot"]
    assert "value_metric" not in allow["toast.rank_snapshot"]
    # plumbing JSON blobs with raw Toast GUIDs
    assert "service_json" not in allow["toast.perf_period"]
    assert "attribution_json" not in allow["toastdm.dm_perf_period"]


def test_get_excluded_columns():
    excl = schema_mod.get_excluded_columns()
    assert excl["toast.time_entry"] >= {"hourly_rate", "tips", "tips_declared"}
    assert excl["appdb.orders"] >= {"customer_phone", "client"}
    assert excl["toast.rank_snapshot"] == {"value_metric"}
    # tables without exclusions are absent (star is allowed there)
    assert "ordersdc.dm_order" not in excl
    assert "daily_sales_summary" not in excl


def test_allowlist_cached_and_stable():
    a1 = schema_mod.get_allowlist()
    a2 = schema_mod.get_allowlist()
    assert a1 is a2
    schema_mod.clear_caches()
    a3 = schema_mod.get_allowlist()
    assert a3 == a1


# ---------------------------------------------------------------------------
# introspection path (synthetic source DB)
# ---------------------------------------------------------------------------

def test_introspection_overrides_static_and_applies_exclusions(monkeypatch, tmp_path):
    db = tmp_path / "synthetic_toast.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE time_entry ("
        "id INTEGER, cena_employee_id INTEGER, store_key TEXT, business_date TEXT,"
        "reg_hours REAL, ot_hours REAL, hourly_rate REAL, tips REAL,"
        "brand_new_col TEXT, contact_email TEXT, badge_hash TEXT)"
    )
    con.commit()
    con.close()
    monkeypatch.setenv("CENA_L3_SRC_TOAST", str(db))
    schema_mod.clear_caches()
    allow = schema_mod.get_allowlist()
    cols = allow["toast.time_entry"]
    assert "brand_new_col" in cols          # live introspection won
    assert "hourly_rate" not in cols        # explicit exclusion still applied
    assert "tips" not in cols
    assert "contact_email" not in cols      # generic email rule
    assert "badge_hash" not in cols         # generic *_hash rule
    # tables absent from the synthetic DB degrade to the static snapshot
    assert "toast.perf_period" in allow
    assert "rank" in allow["toast.rank_snapshot"]
    # other aliases (missing files) fully fall back
    assert "ordersdc.dm_order" in allow


def test_missing_sources_never_raise():
    # autouse fixture already points everything at missing files
    allow = schema_mod.get_allowlist()
    assert "appdb.orders" in allow and "driverdc.dm_pay" in allow
    assert schema_mod.get_schema_context()


# ---------------------------------------------------------------------------
# get_schema_context
# ---------------------------------------------------------------------------

def test_schema_context_size_bounds():
    ctx = schema_mod.get_schema_context()
    assert isinstance(ctx, str)
    assert 2000 < len(ctx) < 16000


def test_schema_context_mentions_critical_gotchas():
    ctx = schema_mod.get_schema_context()
    # net vs gross
    assert "caterer_total_due" in ctx
    assert "NET" in ctx
    assert "GROSS" in ctx
    # future delivery dates filter
    assert "FUTURE" in ctx
    assert "date('now','localtime')" in ctx
    # appdb.orders tiny vs ordersdc rich
    assert "~8 rows" in ctx
    # store-key conventions incl raw store_N mapping
    assert "copperfield" in ctx and "tomball" in ctx
    assert "store_1" in ctx and "store_4" in ctx
    # labor canon + labor_pct semantics + snapshots
    assert "2026-05-11" in ctx and "2026-05-04" in ctx
    assert "CATERING net" in ctx
    assert "SNAPSHOT" in ctx


def test_schema_context_lists_allowlisted_tables_only():
    ctx = schema_mod.get_schema_context()
    allow = schema_mod.get_allowlist()
    for tname in schema_mod.ANALYTICS_TABLES:
        assert tname in ctx
    for key in ("ordersdc.dm_order", "toast.time_entry", "toastdm.dm_schedule",
                "appdb.orders", "driverdc.dm_delivery"):
        assert key in ctx
    # excluded material never appears
    assert "perf_internal" not in ctx
    assert "dm_internal_sales" not in ctx
    assert "hourly_rate" not in ctx
    assert "password_hash" not in ctx
    # every column named in a cols: line is allowlisted
    for line in ctx.splitlines():
        if line.strip().startswith("cols: "):
            cols = {c.strip() for c in line.strip()[len("cols: "):].split(",")}
            assert any(cols <= allow[k] for k in allow), f"orphan cols line: {line}"


def test_schema_context_enriched_by_analytics_schema_doc(monkeypatch):
    fake = types.SimpleNamespace(SCHEMA_DOC="ANALYTICS_SENTINEL_DOC " + "x" * 50)
    monkeypatch.setitem(sys.modules, "app.services.cena_sql_analytics", fake)
    schema_mod.clear_caches()
    ctx = schema_mod.get_schema_context()
    assert "ANALYTICS_SENTINEL_DOC" in ctx


def test_schema_context_truncates_oversized_analytics_doc(monkeypatch):
    fake = types.SimpleNamespace(SCHEMA_DOC="DOCSTART " + "y" * 50000)
    monkeypatch.setitem(sys.modules, "app.services.cena_sql_analytics", fake)
    schema_mod.clear_caches()
    ctx = schema_mod.get_schema_context()
    assert "DOCSTART" in ctx
    assert len(ctx) < 16000


def test_schema_context_survives_broken_analytics_import(monkeypatch):
    class _Boom:
        def __getattr__(self, name):  # pragma: no cover - attribute probe
            raise RuntimeError("half-built module")

    monkeypatch.setitem(sys.modules, "app.services.cena_sql_analytics", _Boom())
    schema_mod.clear_caches()
    ctx = schema_mod.get_schema_context()
    assert "daily_sales_summary" in ctx  # fell back to section-4 text


# ---------------------------------------------------------------------------
# optional real-data smoke (read-only; skipped wherever sources are absent)
# ---------------------------------------------------------------------------

_REAL_ORDERSDC = r"C:\Users\sam\cena-driverdc\_live\ordersdc.sqlite"


@pytest.mark.skipif(not os.path.exists(_REAL_ORDERSDC), reason="real ordersdc absent")
def test_real_ordersdc_introspection_matches_static():
    live = schema_mod._introspect_alias("ordersdc", _REAL_ORDERSDC)
    assert live is not None
    assert set(live["dm_order"]) >= {
        "external_order_id", "caterer_total_due", "ezcater_total", "delivery_date"}
