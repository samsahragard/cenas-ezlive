"""CLI: build cena_analytics.db and optionally spot-check it against raw snapshots.

Usage:
    python scripts/build_cena_analytics.py [--data-dir C:\\Users\\sam\\cena-l3data] [--check]

--data-dir is the BASE data dir (CENA_L3_DATA_DIR semantics); snapshots live in
<data-dir>\\snapshots. --check runs hand-written reference SQL DIRECTLY against
the raw snapshot files (independent of the builder's aggregation code) and
compares against the built analytics tables, printing a pass/fail table.
Exit code 0 = build ok and all checks passed/skipped, 1 = a check failed.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import cena_sql_analytics  # noqa: E402
from app.services.cena_sql_analytics import (  # noqa: E402
    ANALYTICS_DB_NAME,
    LABOR_SNAPSHOT,
    SALES_SNAPSHOT,
    _connect_ro,
    _today_local,
)

log = logging.getLogger("build_cena_analytics")

# Hand-written reference fragments (kept independent of the builder's Python
# aggregation on purpose - this is the spot-check gate).
_STORE_CASE = (
    "CASE WHEN store_key IN ('store_1','store_3','copperfield') THEN 'copperfield'"
    " WHEN store_key IN ('store_2','store_4','tomball') THEN 'tomball'"
    " ELSE store_key END"
)
_NOT_CANCELLED = (
    "lower(coalesce(status,'')) NOT IN"
    " ('cancelled','canceled','no_show','noshow','voided','rejected','declined')"
)
_TOL = 1e-6


def _approx(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= _TOL * max(1.0, abs(float(a)), abs(float(b)))


class _Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failed = 0

    def add(self, name: str, ok: bool | None, detail: str = "") -> None:
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        if ok is False:
            self.failed += 1
        self.rows.append((name, status, detail))

    def print(self) -> None:
        width = max((len(r[0]) for r in self.rows), default=10) + 2
        print()
        print(f"{'CHECK':<{width}}STATUS  DETAIL")
        print("-" * (width + 40))
        for name, status, detail in self.rows:
            print(f"{name:<{width}}{status:<8}{detail}")
        print("-" * (width + 40))
        print("RESULT:", "FAIL" if self.failed else "PASS",
              f"({len(self.rows)} checks, {self.failed} failed)")


def run_checks(snap_dir: Path, analytics_path: Path) -> bool:
    report = _Report()
    adb = _connect_ro(analytics_path)
    sales_raw = snap_dir / SALES_SNAPSHOT
    labor_raw = snap_dir / LABOR_SNAPSHOT
    today_iso = _today_local().isoformat()

    # --- sales: per-store all-time totals -------------------------------
    if sales_raw.exists():
        raw = _connect_ro(sales_raw)
        ref = dict(raw.execute(
            f"SELECT {_STORE_CASE} s, SUM(caterer_total_due) FROM dm_order"
            f" WHERE {_NOT_CANCELLED} GROUP BY s"
        ).fetchall())
        got = dict(adb.execute(
            "SELECT store_key, SUM(net_sales) FROM daily_sales_summary GROUP BY store_key"
        ).fetchall())
        for store in sorted(set(ref) | set(got)):
            report.add(
                f"net_sales total [{store}]",
                _approx(ref.get(store), got.get(store)),
                f"raw={ref.get(store)!r} analytics={got.get(store)!r}",
            )
        ref_orders = dict(raw.execute(
            f"SELECT {_STORE_CASE} s, COUNT(*) FROM dm_order WHERE {_NOT_CANCELLED}"
            " AND delivery_date IS NOT NULL AND delivery_date <> '' GROUP BY s"
        ).fetchall())
        got_orders = dict(adb.execute(
            "SELECT store_key, SUM(order_count) FROM daily_sales_summary GROUP BY store_key"
        ).fetchall())
        for store in sorted(set(ref_orders) | set(got_orders)):
            report.add(
                f"order_count total [{store}]",
                ref_orders.get(store) == got_orders.get(store),
                f"raw={ref_orders.get(store)!r} analytics={got_orders.get(store)!r}",
            )

        # --- sales: sample store/date rows (top 3 by order_count) -------
        samples = adb.execute(
            "SELECT store_key, business_date, net_sales, gross_sales, order_count"
            " FROM daily_sales_summary ORDER BY order_count DESC, business_date LIMIT 3"
        ).fetchall()
        for store, bdate, net, gross, orders in samples:
            ref_row = raw.execute(
                f"SELECT SUM(caterer_total_due), SUM(ezcater_total), COUNT(*)"
                f" FROM dm_order WHERE {_NOT_CANCELLED} AND delivery_date = ?"
                f" AND {_STORE_CASE} = ?",
                (bdate, store),
            ).fetchone()
            ok = (_approx(ref_row[0], net) and _approx(ref_row[1], gross)
                  and ref_row[2] == orders)
            report.add(
                f"daily sample [{store} {bdate}]", ok,
                f"raw(net,gross,n)={tuple(ref_row)!r} analytics={(net, gross, orders)!r}",
            )
        raw.close()
    else:
        report.add("sales checks", None, f"{SALES_SNAPSHOT} missing")

    # --- labor: per-store cost/hour totals ------------------------------
    if labor_raw.exists():
        raw = _connect_ro(labor_raw)
        ref = {
            s: (cost, hours)
            for s, cost, hours in raw.execute(
                f"SELECT {_STORE_CASE} s,"
                " SUM(reg_hours*hourly_rate + 1.5*ot_hours*hourly_rate),"
                " SUM(coalesce(reg_hours,0)+coalesce(ot_hours,0))"
                " FROM time_entry GROUP BY s"
            ).fetchall()
        }
        got = {
            s: (cost, hours)
            for s, cost, hours in adb.execute(
                "SELECT store_key, SUM(labor_cost), SUM(total_hours)"
                " FROM daily_labor_summary GROUP BY store_key"
            ).fetchall()
        }
        for store in sorted(set(ref) | set(got)):
            r, g = ref.get(store, (None, None)), got.get(store, (None, None))
            report.add(
                f"labor totals [{store}]",
                _approx(r[0], g[0]) and _approx(r[1], g[1]),
                f"raw(cost,hours)={r!r} analytics={g!r}",
            )
        raw.close()
    else:
        report.add("labor checks", None, f"{LABOR_SNAPSHOT} missing")

    # --- weekly rollups vs sum of dailies (dates <= today) --------------
    weekly = dict(adb.execute(
        "SELECT store_key, SUM(net_sales) FROM weekly_rollups GROUP BY store_key"
    ).fetchall())
    daily = dict(adb.execute(
        "SELECT store_key, SUM(net_sales) FROM daily_sales_summary"
        " WHERE business_date <= ? GROUP BY store_key",
        (today_iso,),
    ).fetchall())
    for store in sorted(set(weekly) | set(daily)):
        report.add(
            f"weekly==sum(daily) net [{store}]",
            _approx(weekly.get(store), daily.get(store)),
            f"weekly={weekly.get(store)!r} dailies<=today={daily.get(store)!r}",
        )
    weekly_cost = dict(adb.execute(
        "SELECT store_key, SUM(labor_cost) FROM weekly_rollups GROUP BY store_key"
    ).fetchall())
    daily_cost = dict(adb.execute(
        "SELECT store_key, SUM(labor_cost) FROM daily_labor_summary"
        " WHERE business_date <= ? GROUP BY store_key",
        (today_iso,),
    ).fetchall())
    for store in sorted(set(weekly_cost) | set(daily_cost)):
        report.add(
            f"weekly==sum(daily) labor [{store}]",
            _approx(weekly_cost.get(store), daily_cost.get(store)),
            f"weekly={weekly_cost.get(store)!r} dailies<=today={daily_cost.get(store)!r}",
        )

    adb.close()
    report.print()
    return report.failed == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("CENA_L3_DATA_DIR", cena_sql_analytics.DEFAULT_DATA_DIR),
        help="base data dir (snapshots live in <data-dir>\\snapshots)",
    )
    parser.add_argument("--check", action="store_true",
                        help="run reference-SQL spot checks after building")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    snap_dir = Path(args.data_dir) / "snapshots"
    built = cena_sql_analytics.build_analytics_db(str(snap_dir))
    log.info("analytics db built: %s", built)
    if args.check:
        ok = run_checks(snap_dir, Path(built))
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
