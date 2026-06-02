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


CRED_PATH = r"C:\Users\sam\cena-secrets\toast_render_env.txt"
WORKTREE = r"C:\Users\sam\_schedv2_wt"

# Non-sales performance metrics allowed into the employee payload (WHITELIST --
# anything not listed stays internal, so a sales-$ field can never leak). Used
# once server-performance is folded in (Phase 3, with samai's grep gate).
SAFE_PERF_KEYS = {"orders", "order_count", "items", "entrees", "guests",
                  "avg_prep_min", "avg_ticket_time", "void_count", "refund_count"}


def _load_creds(path=CRED_PATH):
    """Parse `export KEY=VALUE` lines into os.environ. Returns KEY NAMES only."""
    names = []
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")
        names.append(k.strip())
    return names


def _pull_period(client, store, guid, toast_id, start, end):
    """Reuse the DEPLOYED filter (toast_link_routes.toast_employee_summary):
    keep entries whose employeeReference.guid == toast_id (GUID attribution),
    sum reg/ot hours + gross (reg*wage + 1.5*ot*wage). No sales in this source."""
    s = dt.datetime.combine(start, dt.time()); e = dt.datetime.combine(end, dt.time())
    entries = client.fetch_time_entries(store, guid, s, e) or []
    reg = ot = cost = tips = 0.0
    wage_seen = False; cards = 0; tip_entries = 0; tip_null = 0
    for te in entries:
        if te.get("deleted"):
            continue
        if (te.get("employeeReference") or {}).get("guid") != toast_id:
            continue
        r = float(te.get("regularHours") or 0); o = float(te.get("overtimeHours") or 0)
        reg += r; ot += o; cards += 1
        w = te.get("hourlyWage")
        if w is not None:
            wage_seen = True; cost += r * float(w) + o * float(w) * 1.5
        # tips: GUID-keyed on the TimeEntry per aick #2921 (no name-match). NULL is
        # genuine (server didn't declare), counted -- never silently mis-attributed.
        nc = te.get("nonCashTips"); dc = te.get("declaredCashTips")
        if nc is None and dc is None:
            tip_null += 1
        else:
            tips += float(nc or 0) + float(dc or 0); tip_entries += 1
    return {"reg": round(reg, 2), "ot": round(ot, 2), "hours": round(reg + ot, 2),
            "gross": round(cost, 2) if wage_seen else None, "cards": cards,
            "tips": round(tips, 2), "tip_entries": tip_entries, "tip_null": tip_null}


def real_run(execute=False):
    """Live Yadira-only refresh. Without execute=True this is a SELF-TEST: loads
    creds + imports the deployed Toast client + prints period windows, with NO
    Toast call and NO write -- proves readiness while the lane is gated."""
    import sys
    if WORKTREE not in sys.path:
        sys.path.insert(0, WORKTREE)
    names = _load_creds()
    print("=" * 60)
    print("PHASE 2 REAL-RUN %s -- Yadira" % ("EXECUTE" if execute else "SELF-TEST"))
    print("=" * 60)
    print("creds loaded (key names): " + ", ".join(sorted(names)))
    from app.services.toast_client import ToastClient, restaurant_guids
    guid = restaurant_guids().get(YADIRA["store_key"])
    print("Yadira: cena_id=%s toast_id=%s store=%s restaurant_guid_present=%s" % (
        YADIRA["cena_employee_id"], YADIRA["toast_id"], YADIRA["store_key"], bool(guid)))
    today = dt.date.today(); wins = period_windows(today)
    print("period windows:")
    for p, (s, e) in wins.items():
        print("  %-7s %s .. %s" % (p, s, e))
    if not execute:
        print("-" * 60)
        print("SELF-TEST PASS -- creds parse + deployed client import + windows OK.")
        print("Holding the live Toast pull for samai branch-audit PASS + Sam green-light")
        print("(run with --execute once cleared).")
        print("=" * 60)
        return
    client = ToastClient.shared()
    con = sqlite3.connect(DB)
    started = dt.datetime.now().isoformat(timespec="seconds")
    written = 0
    try:
        for p, win in wins.items():
            agg = _pull_period(client, YADIRA["store_key"], guid, YADIRA["toast_id"], win[0], win[1])
            pp = {
                "cena_employee_id": YADIRA["cena_employee_id"],
                "toast_employee_id": YADIRA["toast_id"], "store_key": YADIRA["store_key"],
                "period": p, "period_start": win[0].isoformat(), "period_end": win[1].isoformat(),
                "reg_hours": agg["reg"], "ot_hours": agg["ot"], "total_hours": agg["hours"],
                "base_pay": agg["gross"] or 0, "tips": agg["tips"], "tip_pct": None,
                "service_json": json.dumps({
                    "timecards": agg["cards"],
                    "attribution_method": "guid_direct",          # Sam #2923
                    "guid_key": "timeEntries.employeeReference.guid == cena_toast_link.toast_id",
                    "toast_guid": YADIRA["toast_id"],
                    "guid_attributed": ["reg_hours", "ot_hours", "total_hours", "base_pay", "tips"],
                    "tip_entries": agg["tip_entries"],
                    "unattributed_null": {"tips_not_declared": agg["tip_null"]},
                    "service_metrics": "deferred_v2 (needs server_guid carried out of "
                                       "toast_reports.server_perf_report:499; NO name_fallback used)",
                }),
                "attendance_json": None, "rank_in_store": None, "rank_metric": None,
                "computed_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
            _write(con, pp, None)   # perf_internal None -- v1 pulls NO sales source
            written += 1
            print("  [%-6s] %s..%s hours=%.2f gross=%s cards=%d" % (
                p, win[0], win[1], agg["hours"], agg["gross"], agg["cards"]))
        con.execute("INSERT INTO sync_run (started_at,finished_at,scope,period,status,"
                    "employees_processed,rows_written,note) VALUES (?,?,?,?,?,?,?,?)",
                    (started, dt.datetime.now().isoformat(timespec="seconds"), "yadira", "all",
                     "ok", 1, written, "live GUID pull v1 (hours/pay/tips; service deferred v2)"))
        con.commit()
        # ---- PROOF read-back (Sam #2928) ----
        print("-" * 60)
        print("PROOF (read back from perf.sqlite):")
        for r in con.execute(
                "SELECT period,period_start,period_end,total_hours,reg_hours,ot_hours,"
                "base_pay,tips,toast_employee_id,service_json FROM perf_period "
                "WHERE cena_employee_id=? ORDER BY period", (YADIRA["cena_employee_id"],)):
            sj = json.loads(r[9] or "{}")
            print("  [%-6s] %s..%s hrs=%.2f (reg %.2f/ot %.2f) pay=%.2f tips=%.2f method=%s null_tips=%s" % (
                r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7],
                sj.get("attribution_method"), (sj.get("unattributed_null") or {}).get("tips_not_declared")))
        print("  GUID anchor: cena_employee_id=%s <-> toast_id=%s (every row)" % (
            YADIRA["cena_employee_id"], YADIRA["toast_id"]))
        counts = {t: con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                  for t in ("perf_period", "perf_internal", "time_entry", "sync_run")}
        print("  row counts: " + ", ".join("%s=%d" % (k, v) for k, v in counts.items()))
        print("  perf_internal=%d (v1 pulls NO sales source -> expected 0 = sales-safe)" % counts["perf_internal"])
        sr = con.execute("SELECT id,started_at,finished_at,scope,status,rows_written,note "
                         "FROM sync_run ORDER BY id DESC LIMIT 1").fetchone()
        print("  sync_run: id=%s %s..%s scope=%s status=%s rows=%s note=%s" % sr)
    finally:
        con.close()
    print("-" * 60)
    print("EXECUTE done -- Yadira v1 written to CK-local perf.sqlite (NO prod write).")
    print("=" * 60)


def push(base_url="https://cenas-ezlive.onrender.com"):
    """Phase 3 (Sam #2938/#2941): POST the SANITIZED Yadira perf_period rows to the
    app token-gated /cron/perf-push. Each period is split into employee-visible
    'service' ({} in v1 -- service metrics deferred) + INTERNAL 'attribution'
    (the receiver stores it out of the employee payload). Reads CRON_TOKEN from
    env. NO sales pushed (perf_period carries none)."""
    import urllib.request
    token = os.getenv("CRON_TOKEN")
    if not token:
        raise SystemExit("CRON_TOKEN not in env -- export it before --push")
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM perf_period WHERE cena_employee_id=? ORDER BY period",
                       (YADIRA["cena_employee_id"],)).fetchall()
    con.close()
    if not rows:
        raise SystemExit("no perf_period rows -- run --execute first")
    periods = []
    for r in rows:
        try:
            attribution = json.loads(r["service_json"] or "{}")
        except Exception:
            attribution = {}
        periods.append({
            "period": r["period"], "period_start": r["period_start"], "period_end": r["period_end"],
            "total_hours": r["total_hours"], "reg_hours": r["reg_hours"], "ot_hours": r["ot_hours"],
            "base_pay": r["base_pay"], "tips": r["tips"],
            "service": {},               # employee-visible service metrics -- none in v1
            "attribution": attribution,  # INTERNAL -- receiver keeps it out of the payload
            "computed_at": r["computed_at"],
        })
    payload = {"employee": {"cena_employee_id": YADIRA["cena_employee_id"],
                            "toast_id": YADIRA["toast_id"], "store_key": YADIRA["store_key"]},
               "periods": periods}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base_url + "/cron/perf-push", data=data,
                                 headers={"Content-Type": "application/json", "X-Cron-Token": token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = resp.read().decode("utf-8"); code = resp.getcode()
    print("PUSH ->", base_url + "/cron/perf-push", "HTTP", code)
    print("response:", out)
    print("pushed periods:", [p["period"] for p in periods],
          "(service={} employee-visible; attribution kept internal)")


if __name__ == "__main__":
    import sys
    if "--execute" in sys.argv:
        real_run(execute=True)
    elif "--self-test" in sys.argv:
        real_run(execute=False)
    elif "--push" in sys.argv:
        push()
    else:
        dry_run()
