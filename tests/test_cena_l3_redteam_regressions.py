"""Regression tests for the Wave 1 adversarial red-team findings.

Each test pins a specific confirmed vulnerability/defect so it cannot silently
return. IDs map to the red-team report:
  VAL-001 (critical) - unqualified excluded column bypass via a no-op opaque source
  EXC-01  (high)     - 2 MB size cap was a post-append check (single oversized row)
  EXC-02  (medium)   - corrupt snapshot leaked raw sqlite3.DatabaseError
  CONF-1  (medium)   - SCHEMA_DOC truncation dropped same_day_lastweek + anomaly_flags
  CONF-2  (medium)   - non-overlapping sales/labor windows -> labor_pct NULL, undocumented
  CONF-3  (low)      - "every table has built_at" false for the same_day_lastweek VIEW

These exercise the real Wave 1 modules. The executor/schema tests that need a
built analytics db + snapshots are guarded with skipif so the suite stays
hermetic on CI (where C:\\Users\\sam\\cena-l3data does not exist); the pure
validator and schema-context tests run everywhere.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.services import cena_sql_schema as sch
from app.services.cena_sql_validator import validate_sql

_DATA_DIR = os.environ.get("CENA_L3_DATA_DIR", r"C:\Users\sam\cena-l3data")
_SNAP_DIR = Path(_DATA_DIR) / "snapshots"
_HAS_SNAPSHOTS = (_SNAP_DIR / "cena_analytics.db").exists() and (
    _SNAP_DIR / "ordersdc.sqlite"
).exists()
_needs_snapshots = pytest.mark.skipif(
    not _HAS_SNAPSHOTS, reason="real snapshots not provisioned on this host"
)


# --------------------------------------------------------------------------- #
# VAL-001 - the critical one. Pure-validator, runs everywhere.
# --------------------------------------------------------------------------- #
_BYPASS_VECTORS = [
    # excluded column, unqualified, with a no-op opaque source in FROM
    "SELECT hourly_rate FROM toast.time_entry, (SELECT 1 AS k) d",
    "SELECT tips FROM toast.time_entry, (SELECT 1 AS k) d",
    "WITH d AS (SELECT 1 AS k) SELECT hourly_rate FROM toast.time_entry, d",
    "SELECT phone FROM appdb.drivers, (SELECT 1 AS k) d",
    "SELECT password_hash FROM appdb.drivers, (SELECT 1 AS k) d",
    "SELECT client FROM appdb.orders, (SELECT 1 AS k) d",
    "SELECT delivery_address FROM appdb.orders, (SELECT 1 AS k) d",
    "SELECT value_metric FROM toast.rank_snapshot, (SELECT 1 AS k) d",
    "SELECT base_pay FROM toastdm.dm_time_entry, (SELECT 1 AS k) d",
    # json_each opaque source variant
    "SELECT hourly_rate FROM toast.time_entry, json_each('[1]')",
    # inference oracle: excluded column in WHERE
    "SELECT business_date FROM toast.time_entry, (SELECT 1 AS k) d WHERE hourly_rate > 20",
    # ... and in ORDER BY / GROUP BY
    "SELECT business_date FROM toast.time_entry, (SELECT 1 AS k) d ORDER BY hourly_rate",
    "SELECT COUNT(*) FROM toast.time_entry, (SELECT 1 AS k) d GROUP BY tips",
]


@pytest.mark.parametrize("sql", _BYPASS_VECTORS)
def test_val001_excluded_column_bypass_blocked(sql):
    ok, reason = validate_sql(sql)
    assert ok is False, f"VAL-001 regression: bypass accepted -> {sql!r}"
    assert "excluded by policy" in reason or "not found" in reason, reason


_LEGIT_WITH_OPAQUE = [
    # an ALLOWED column alongside an opaque source must still pass
    "SELECT business_date FROM toast.time_entry, (SELECT 1 AS k) d",
    "SELECT reg_hours, ot_hours FROM toast.time_entry, (SELECT 1 AS k) d",
    # a column that only exists on the CTE/opaque source
    "WITH d AS (SELECT 1 AS k) SELECT k FROM d",
    # plain allowed analytics query
    "SELECT store_key, net_sales FROM daily_sales_summary",
]


@pytest.mark.parametrize("sql", _LEGIT_WITH_OPAQUE)
def test_val001_fix_does_not_overblock_legit(sql):
    ok, reason = validate_sql(sql)
    assert ok is True, f"VAL-001 fix over-blocked a legit query {sql!r}: {reason}"


# --------------------------------------------------------------------------- #
# EXC-01 / EXC-02 - executor, need provisioned snapshots.
# --------------------------------------------------------------------------- #
@_needs_snapshots
def test_exc01_single_oversized_row_capped():
    from app.services.cena_sql_executor import run_readonly_sql, SIZE_CAP_BYTES

    # one ~50 MB cell must not slip past the 2 MB cap
    out = run_readonly_sql("SELECT printf('%.*c', 50000000, 'A') AS blob")
    assert out["truncated"] is True
    assert out["row_count"] == 0  # the over-cap row is refused, not buffered whole
    # total returned payload stays within the cap budget
    buffered = sum(len(str(r)) for r in out["rows"])
    assert buffered <= SIZE_CAP_BYTES


@_needs_snapshots
def test_exc01_normal_query_unaffected():
    from app.services.cena_sql_executor import run_readonly_sql

    out = run_readonly_sql("SELECT store_key, net_sales FROM daily_sales_summary LIMIT 3")
    assert out["truncated"] is False
    assert out["row_count"] == 3


@_needs_snapshots
def test_exc02_corrupt_snapshot_raises_cena_error(tmp_path):
    from app.services.cena_sql_executor import run_readonly_sql, CenaSqlError

    snap = tmp_path / "snapshots"
    snap.mkdir()
    shutil.copy2(_SNAP_DIR / "cena_analytics.db", snap / "cena_analytics.db")
    (snap / "ordersdc.sqlite").write_bytes(b"garbage not a db" * 5000)
    with pytest.raises(CenaSqlError):
        run_readonly_sql("SELECT 1", _data_dir=str(tmp_path))


# --------------------------------------------------------------------------- #
# CONF-1 / CONF-2 / CONF-3 - schema context truthfulness. Pure, runs everywhere.
# --------------------------------------------------------------------------- #
def test_conf1_all_analytics_tables_documented():
    sch.clear_caches()
    try:
        ctx = sch.get_schema_context()
    finally:
        sch.clear_caches()
    body = ctx[ctx.find("## ANALYTICS") : ctx.find("## ordersdc")]
    # every analytics table (incl. the two that used to be truncated out) must
    # appear with its column list in the section the reasoner consumes
    for t in (
        "daily_sales_summary",
        "daily_labor_summary",
        "weekly_rollups",
        "item_sales_summary",
        "daypart_comparison",
        "same_day_lastweek",
        "anomaly_flags",
    ):
        assert t in body, f"CONF-1 regression: {t} missing from schema context"
    # anomaly_flags must keep its metric-domain semantics, the load-bearing detail
    assert "anomaly_flags" in body and "store_key" in body
    # same_day_lastweek view column must be present (it carries no built_at)
    assert "prev_week_net_sales" in body or "day_of_week" in body


def test_conf1_context_under_16k():
    sch.clear_caches()
    try:
        ctx = sch.get_schema_context()
    finally:
        sch.clear_caches()
    assert len(ctx) < 16000, f"context too large: {len(ctx)}"


def test_conf2_non_overlap_gotcha_documented():
    sch.clear_caches()
    try:
        ctx = sch.get_schema_context()
    finally:
        sch.clear_caches()
    low = ctx.lower()
    assert "do not overlap" in low, "CONF-2 regression: non-overlap gotcha missing"
    # and it must be framed as 'unavailable/NULL', not a real ratio
    assert "labor_pct" in ctx and ("null" in low or "unavailable" in low)


def test_conf3_built_at_header_is_accurate():
    sch.clear_caches()
    try:
        ctx = sch.get_schema_context()
    finally:
        sch.clear_caches()
    # the blanket 'every table has built_at' was false for the VIEW
    assert "every BASE table" in ctx
    assert "VIEW has none" in ctx
    # and the validator agrees: the view has no built_at column
    allow = sch.get_allowlist()
    assert "built_at" not in allow["same_day_lastweek"]
    ok, _ = validate_sql("SELECT built_at FROM same_day_lastweek")
    assert ok is False
