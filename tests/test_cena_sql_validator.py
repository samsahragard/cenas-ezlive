"""Tests for app/services/cena_sql_validator.py (Subagent A).

Hermetic: schema sources are pointed at missing files so the allowlist comes
from the static curated fallback; no DBs, no network.
"""
from __future__ import annotations

import pytest

from app.services import cena_sql_schema as schema_mod
from app.services.cena_sql_validator import validate_sql

_ENV_VARS = (
    "CENA_L3_SRC_APPDB", "CENA_L3_SRC_TOAST", "CENA_L3_SRC_TOASTDM",
    "CENA_L3_SRC_ORDERSDC", "CENA_L3_SRC_DRIVERDC",
)


@pytest.fixture(autouse=True)
def _isolated_sources(monkeypatch, tmp_path):
    for env in _ENV_VARS:
        monkeypatch.setenv(env, str(tmp_path / f"missing_{env}.sqlite"))
    schema_mod.clear_caches()
    yield
    schema_mod.clear_caches()


def ok(sql):
    valid, reason = validate_sql(sql)
    assert valid, f"expected OK but got: {reason}\nSQL: {sql}"
    assert reason == ""


def bad(sql, *substrings):
    valid, reason = validate_sql(sql)
    assert not valid, f"expected rejection but passed: {sql}"
    low = reason.lower()
    for s in substrings:
        assert s.lower() in low, f"reason missing {s!r}: {reason}"
    return reason


# ---------------------------------------------------------------------------
# good SELECTs pass
# ---------------------------------------------------------------------------

def test_simple_analytics_select():
    ok("SELECT business_date, net_sales FROM daily_sales_summary "
       "WHERE store_key = 'copperfield' ORDER BY business_date DESC LIMIT 7")


def test_no_limit_is_fine():
    ok("SELECT store_key, SUM(net_sales) FROM daily_sales_summary GROUP BY store_key")


def test_qualified_raw_table_with_history_filter():
    ok("SELECT delivery_date, SUM(caterer_total_due) AS sales "
       "FROM ordersdc.dm_order "
       "WHERE delivery_date <= date('now','localtime') "
       "GROUP BY delivery_date ORDER BY sales DESC")


def test_join_across_attached_schemas():
    ok("SELECT o.delivery_date, d.name, dd.driver_payout "
       "FROM ordersdc.dm_order o "
       "JOIN ordersdc.dm_order_driver od ON od.external_order_id = o.external_order_id "
       "JOIN driverdc.dm_driver d ON d.driver_id = od.driver_id "
       "LEFT JOIN driverdc.dm_delivery dd ON dd.external_order_id = o.external_order_id")


def test_cte_select():
    ok("WITH lab AS (SELECT business_date, SUM(reg_hours + ot_hours) AS hrs "
       "FROM toast.time_entry GROUP BY business_date) "
       "SELECT s.business_date, s.net_sales, lab.hrs "
       "FROM daily_sales_summary s JOIN lab ON lab.business_date = s.business_date")


def test_aggregates_and_count_star():
    ok("SELECT store_key, COUNT(*) AS n, AVG(reg_hours) "
       "FROM toast.time_entry GROUP BY store_key HAVING COUNT(*) > 3")


def test_order_by_select_alias():
    ok("SELECT net_sales AS ns FROM daily_sales_summary ORDER BY ns DESC")


def test_union_of_selects():
    ok("SELECT store_key, business_date FROM daily_sales_summary "
       "UNION ALL SELECT store_key, business_date FROM daily_labor_summary")
    # ORDER BY on the union references output columns
    ok("SELECT store_key FROM daily_sales_summary UNION "
       "SELECT store_key FROM daily_labor_summary ORDER BY store_key")


def test_subquery_in_from():
    ok("SELECT t.d, t.total FROM (SELECT delivery_date AS d, "
       "SUM(caterer_total_due) AS total FROM ordersdc.dm_order GROUP BY 1) t "
       "WHERE t.total > 100")


def test_correlated_scalar_subquery():
    ok("SELECT od.external_order_id, "
       "(SELECT name FROM driverdc.dm_driver d WHERE d.driver_id = od.driver_id) "
       "FROM ordersdc.dm_order_driver od")


def test_three_part_column_names():
    ok("SELECT toast.time_entry.reg_hours FROM toast.time_entry")


def test_select_star_on_analytics_table():
    ok("SELECT * FROM daily_labor_summary WHERE store_key = 'tomball'")
    ok("SELECT s.* FROM anomaly_flags s")


def test_select_star_on_raw_table_without_exclusions():
    ok("SELECT * FROM ordersdc.dm_order_timing")
    ok("SELECT t.* FROM ordersdc.dm_order t")


def test_json_each_table_function():
    # json_each over an allowed *_json column (item modifiers); employee name/positions
    # json are now privacy-excluded, so use the item-modifier blob instead.
    ok("SELECT i.name, j.value FROM ordersdc.dm_order_item i, "
       "json_each(i.modifiers_json) j")


def test_date_functions_and_literals():
    ok("SELECT date('now','localtime'), strftime('%Y-%m-%d','now'), 1 + 2")


# ---------------------------------------------------------------------------
# statement-class rejections
# ---------------------------------------------------------------------------

def test_reject_empty_and_whitespace():
    bad("", "empty")
    bad("   ;  ", "select")


def test_reject_non_select_statements():
    cases = [
        ("INSERT INTO tasks VALUES (1)", "insert"),
        ("UPDATE appdb_orders SET status = 'x' WHERE id = 1", "update"),
        ("DELETE FROM appdb_orders WHERE id = 1", "delete"),
        ("REPLACE INTO t VALUES (1)", "replace"),
        ("CREATE TABLE t (a INT)", "create"),
        ("DROP TABLE appdb_orders", "drop"),
        ("ALTER TABLE t ADD COLUMN c INT", "alter"),
        ("PRAGMA table_info(orders)", "pragma"),
        ("ATTACH DATABASE 'x.db' AS x", "attach"),
        ("DETACH DATABASE x", "detach"),
        ("VACUUM", "vacuum"),
        ("ANALYZE", "analyze"),
        ("REINDEX", "reindex"),
    ]
    for sql, word in cases:
        reason = bad(sql, word)
        assert "select" in reason.lower()


def test_reject_multiple_statements():
    bad("SELECT 1; SELECT 2", "multiple", "one")
    bad("SELECT net_sales FROM daily_sales_summary; PRAGMA temp_store",
        )


def test_trailing_semicolon_is_fine():
    ok("SELECT business_date FROM daily_sales_summary;")


def test_reject_parse_error():
    bad("SELECT FROM WHERE", "parse")


def test_reject_select_into():
    bad("SELECT 1 INTO x", "into")


def test_reject_dangerous_functions():
    bad("SELECT load_extension('evil')", "load_extension")
    bad("SELECT readfile('C:/secrets.txt')", "readfile")
    bad("SELECT writefile('x', name) FROM driverdc.dm_driver", "writefile")
    bad("SELECT lower(edit('x'))", "edit")


# ---------------------------------------------------------------------------
# table allowlist rejections
# ---------------------------------------------------------------------------

def test_reject_non_allowlisted_table():
    reason = bad("SELECT id FROM appdb.users", "appdb.users", "not allowlisted")
    assert "orders" in reason  # actionable: names available appdb tables


def test_reject_isolated_sales_lane():
    bad("SELECT sales_dollars FROM toast.perf_internal", "toast.perf_internal")
    bad("SELECT eligible_sales FROM toastdm.dm_internal_sales",
        "toastdm.dm_internal_sales")


def test_reject_pay_leaking_rank_table():
    bad("SELECT rank_json FROM toastdm.dm_rank", "toastdm.dm_rank")


def test_reject_unknown_schema():
    bad("SELECT a FROM otherdb.things", "unknown schema", "otherdb",
        "appdb", "driverdc")


def test_reject_unqualified_raw_table_with_suggestion():
    bad("SELECT reg_hours FROM time_entry", "toast.time_entry")
    bad("SELECT caterer_total_due FROM dm_order", "ordersdc.dm_order")


def test_reject_unknown_unqualified_table():
    bad("SELECT x FROM nonexistent_table", "nonexistent_table",
        "daily_sales_summary")


def test_reject_sqlite_master():
    bad("SELECT name FROM sqlite_master", "sqlite_master")


# ---------------------------------------------------------------------------
# column rejections
# ---------------------------------------------------------------------------

def test_reject_excluded_column_qualified():
    reason = bad("SELECT te.hourly_rate FROM toast.time_entry te",
                 "hourly_rate", "excluded")
    assert "reg_hours" in reason  # says what IS allowed


def test_reject_excluded_column_unqualified():
    bad("SELECT tips FROM toast.time_entry", "tips", "excluded")
    bad("SELECT customer_phone FROM appdb.orders", "customer_phone", "excluded")
    bad("SELECT base_pay FROM toastdm.dm_time_entry", "base_pay", "excluded")
    bad("SELECT value_metric FROM toast.rank_snapshot", "value_metric", "excluded")


def test_reject_nonexistent_column():
    reason = bad("SELECT bogus_col FROM ordersdc.dm_order o WHERE o.bogus_col > 1",
                 "bogus_col")
    assert "dm_order" in reason


def test_reject_ambiguous_unqualified_column():
    reason = bad(
        "SELECT store_key FROM toast.time_entry te "
        "JOIN toastdm.dm_time_entry dte ON te.business_date = dte.business_date",
        "store_key", "ambiguous")
    assert "toast.time_entry" in reason and "toastdm.dm_time_entry" in reason
    assert "qualify" in reason.lower()


def test_unqualified_single_table_column_ok():
    ok("SELECT store_key FROM toast.time_entry")


def test_reject_unknown_alias():
    bad("SELECT zz.name FROM driverdc.dm_driver d", "zz")


# ---------------------------------------------------------------------------
# SELECT * policy
# ---------------------------------------------------------------------------

def test_reject_star_on_raw_table_with_exclusions_enumerates_allowed():
    reason = bad("SELECT * FROM toast.time_entry", "toast.time_entry")
    for col in ("clock_in", "clock_out", "reg_hours", "ot_hours", "business_date"):
        assert col in reason
    assert "hourly_rate" not in reason  # excluded cols are not advertised


def test_reject_alias_star_on_raw_table_with_exclusions():
    reason = bad("SELECT o.* FROM appdb.orders o", "appdb.orders")
    assert "delivery_date" in reason and "status" in reason
    assert "customer_phone" not in reason


def test_reject_star_on_drivers():
    bad("SELECT * FROM appdb.drivers", "appdb.drivers")


def test_star_inside_count_is_not_select_star():
    ok("SELECT COUNT(*) FROM appdb.drivers")
    ok("SELECT business_date, COUNT(*) FROM toast.time_entry GROUP BY business_date")


def test_star_on_subquery_is_fine():
    ok("SELECT * FROM (SELECT business_date, reg_hours FROM toast.time_entry)")


# ---------------------------------------------------------------------------
# misc contract behaviors
# ---------------------------------------------------------------------------

def test_reason_empty_on_success():
    valid, reason = validate_sql("SELECT 1")
    assert valid is True and reason == ""


def test_returns_tuple_of_bool_str():
    out = validate_sql("SELECT nope FROM nada")
    assert isinstance(out, tuple) and len(out) == 2
    assert isinstance(out[0], bool) and isinstance(out[1], str)


def test_validator_does_not_import_executor_or_analytics():
    import app.services.cena_sql_validator as v
    src = open(v.__file__, encoding="utf-8").read()
    assert "cena_sql_executor" not in src
    assert "cena_sql_analytics" not in src
