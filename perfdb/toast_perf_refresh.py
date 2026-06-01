"""Phase 2 refresh/importer (Sam #2901) -- CK-local, Yadira-only first.

STATUS: dry-run path is COMPLETE and proves the keystone safety property --
restaurant sales are isolated in perf_internal and can NEVER reach the employee
payload (built only from perf_period). The real_run() live-Toast path is a STUB
pending (a) Toast creds on CK and (b) aick's Phase 1 field map, so the exact
field/endpoint wiring matches what is actually deployed ("extend not rebuild").

No prod DB is touched. Dry-run writes to a throwaway temp SQLite, not perf.sqlite.
"""
import sqlite3, os, json, re, tempfile
import datetime as dt

DB_DIR = r"C:\Users\sam\cena-perfdb"
DB = os.path.join(DB_DIR, "perf.sqlite")
SCHEMA = os.path.join(DB_DIR, "schema_v1.sql")

# First test case (Sam): real identity from the live roster (roster-peek id=71).
YADIRA = {"cena_employee_id": 71,
          "toast_id": "75a58c11-bce8-4e03-9222-dec3a3774744",
          "store_key": "copperfield",
          "full_name": "Yadira Romer Hernandez"}

# samai's sanitize criterion: no sales-ish key/value may appear in an employee payload.
SALES_RE = re.compile(r"sales|revenue|net_sales|gross|store_total|comp", re.I)


def period_windows(ref):
    """today / week (Saturday-start, matches app #2603) / month (MTD) / last30."""
    since_sat = (ref.weekday() - 5) % 7        # Mon=0..Sun=6; Saturday=5
    wk_start = ref - dt.timedelta(days=since_sat)
    return {
        "today":  (ref, ref),
        "week":   (wk_start, ref),
        "month":  (ref.replace(day=1), ref),
        "last30": (ref - dt.timedelta(days=29), ref),
    }


def normalize(labor, sales_ctx, period, window):
    """Split a toast_employee_summary-shaped labor payload into a SANITIZED
    perf_period row + an INTERNAL-ONLY perf_internal row. sales_ctx is
    structurally confined to the internal row -- it has no path into perf_period."""
    pay = labor.get("payroll") or {}
    reg = float(pay.get("reg_hours") or 0)
    ot = float(pay.get("ot_hours") or 0)
    base_pay = float(pay.get("reg_pay") or 0) + float(pay.get("ot_pay") or 0)
    tips = float(pay.get("tips") or labor.get("tips") or 0)
    now = dt.datetime.now().isoformat(timespec="seconds")
    perf_period = {
        "cena_employee_id": YADIRA["cena_employee_id"],
        "toast_employee_id": YADIRA["toast_id"],
        "store_key": YADIRA["store_key"],
        "period": period,
        "period_start": window[0].isoformat(),
        "period_end": window[1].isoformat(),
        "reg_hours": reg, "ot_hours": ot, "total_hours": round(reg + ot, 2),
        "base_pay": round(base_pay, 2), "tips": round(tips, 2),
        "tip_pct": None,        # sales-derived -> classified INTERNAL pending aick/samai
        "service_json": json.dumps(labor.get("performance") or {}),
        "attendance_json": None,
        "rank_in_store": None, "rank_metric": None,
        "computed_at": now,
    }
    perf_internal = None
    if sales_ctx:
        perf_internal = {
            "cena_employee_id": YADIRA["cena_employee_id"],
            "store_key": YADIRA["store_key"], "period": period,
            "sales_dollars": float(sales_ctx.get("sales_dollars") or 0),
            "sales_attributed": float(sales_ctx.get("sales_attributed") or 0),
            "scoring_json": json.dumps(sales_ctx.get("scoring") or {}),
            "computed_at": now,
        }
    return perf_period, perf_internal


def employee_payload(pp):
    """EXACTLY what the app read-cache would receive -- built ONLY from
    perf_period columns. No perf_internal field is reachable from here."""
    return {
        "period": pp["period"], "period_start": pp["period_start"], "period_end": pp["period_end"],
        "total_hours": pp["total_hours"], "reg_hours": pp["reg_hours"], "ot_hours": pp["ot_hours"],
        "base_pay": pp["base_pay"], "tips": pp["tips"],
        "service": json.loads(pp["service_json"] or "{}"),
    }


def _temp_db():
    ddl = open(SCHEMA, encoding="utf-8").read()
    fd, path = tempfile.mkstemp(suffix="_perfdry.sqlite"); os.close(fd)
    con = sqlite3.connect(path); con.executescript(ddl); con.commit()
    return con, path


def _write(con, pp, pi):
    cols = ",".join(pp); con.execute(
        "INSERT OR REPLACE INTO perf_period (%s) VALUES (%s)" % (cols, ",".join("?" * len(pp))),
        list(pp.values()))
    if pi:
        cols2 = ",".join(pi); con.execute(
            "INSERT OR REPLACE INTO perf_internal (%s) VALUES (%s)" % (cols2, ",".join("?" * len(pi))),
            list(pi.values()))
    con.commit()


def dry_run():
    # SAMPLE Toast labor (summary-shaped) + an INJECTED sales figure to PROVE
    # that even when sales is present at input, it cannot reach the employee payload.
    sample_labor = {"ok": True, "hours": 31.0,
                    "payroll": {"reg_hours": 29.0, "ot_hours": 2.0,
                                "reg_pay": 406.0, "ot_pay": 42.0, "tips": 63.5},
                    "performance": {"available": True, "orders": 38, "avg_prep_min": 6.4},
                    "timecards": []}
    sample_sales = {"sales_dollars": 5123.40, "sales_attributed": 980.25,
                    "scoring": {"composite": 0.81}}
    today = dt.date.today()
    wins = period_windows(today)
    con, path = _temp_db()
    print("=" * 60)
    print("PHASE 2 DRY-RUN (Sam #2901) -- Yadira, sample data, temp DB")
    print("=" * 60)
    print("temp DB     :", path, "(throwaway -- perf.sqlite untouched)")
    print("Yadira      : cena_id=%s toast_id=%s store=%s" % (
        YADIRA["cena_employee_id"], YADIRA["toast_id"], YADIRA["store_key"]))
    print("period windows (proof of date logic):")
    for p, (s, e) in wins.items():
        print("  %-7s %s .. %s" % (p, s, e))
    overall = True
    for p, win in wins.items():
        pp, pi = normalize(sample_labor, sample_sales, p, win)
        _write(con, pp, pi)
        ok, hits = (lambda b: (not SALES_RE.search(b), SALES_RE.findall(b)))(json.dumps(employee_payload(pp)))
        overall = overall and ok
        print("  [%-6s] employee payload sanitized=%s%s" % (
            p, ok, "" if ok else "  LEAK:" + str(hits)))
    pp_n = con.execute("SELECT COUNT(*) FROM perf_period").fetchone()[0]
    pi_n = con.execute("SELECT COUNT(*) FROM perf_internal").fetchone()[0]
    sales_cols = con.execute(
        "SELECT COUNT(*) FROM pragma_table_info('perf_period') WHERE LOWER(name) LIKE '%sales%'").fetchone()[0]
    sample_internal = con.execute("SELECT sales_dollars FROM perf_internal LIMIT 1").fetchone()
    print("-" * 60)
    print("rows written: perf_period=%d  perf_internal=%d" % (pp_n, pi_n))
    print("perf_period 'sales' columns: %d (0 = sales structurally absent)" % sales_cols)
    print("perf_internal sales_dollars: %s  (INTERNAL ONLY -- never pushed)" % (
        sample_internal[0] if sample_internal else None))
    print("sample employee payload (week):")
    print("  " + json.dumps(employee_payload(normalize(sample_labor, sample_sales, "week", wins["week"])[0])))
    con.close(); os.remove(path)
    print("-" * 60)
    print("RESULT      :", "PASS -- sales isolated; every employee payload sanitized"
          if overall and sales_cols == 0 else "FAIL")
    print("=" * 60)


def real_run(*_a, **_k):
    raise NotImplementedError(
        "real_run pending: (1) Toast creds on CK (TOAST_CLIENT_ID/SECRET + "
        "RESTAURANT_GUID_{COPPERFIELD,TOMBALL}), (2) aick Phase 1 field map to "
        "lock exact field/endpoint wiring. Dry-run path is complete.")


if __name__ == "__main__":
    import sys
    if "--real" in sys.argv:
        real_run()
    else:
        dry_run()
