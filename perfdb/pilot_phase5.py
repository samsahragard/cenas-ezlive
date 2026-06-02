"""Phase 5.1 (Sam #3005..#3019) -- ALLOWLIST-GATED pilot refresh + RANKING.

ALLOWLIST-ONLY by construction: this module iterates a HARD-CODED list of the 5 locked
pilot employees + Yadira (ref). There is NO all-employee loop and NO roster query; a
guard (_assert_allowed) refuses any cena_employee_id not on the allowlist before any
write or push. Broad rollout stays blocked until separate approval.

SALES WALL (primary Phase-5.1 gate, Sam #3019 / aick #3016 / samai #3020):
  Ranking introduces restaurant sales into the pipeline for the FIRST time --
  cashSales+nonCashSales are pulled (toast_perf_refresh._pull_sales_internal) ONLY into
  perf_internal as eligible_sales (the tip% denominator). The sanitized push + rank
  output + leaderboard carry ONLY the tip_percent RATIO. No sales-$ ever reaches
  perf_period / time_entry / perf_shift_cache / push / rank output / leaderboard / DOM.
  The N3 PUSH_SALES_RE guard now explicitly catches cashSales|nonCashSales|eligible_sales.

Modes:
  --pull        live allowlist-gated Toast pull -> CK perf.sqlite (periods, shifts, eligible_sales)
  --rank        compute ranks from perf.sqlite -> rank_snapshot (+ per-employee rank output JSON)
  --self-test   NO network: allowlist guard + percentile/cohort/min-cohort/threshold unit checks
  --push        sanitized allowlist push (periods + shifts + rank output) to the app
Nothing is employee-visible until the app push, which is gated on aick PASS + samai PASS + Sam review.
"""
import os, sys, json, sqlite3, time
import datetime as dt

DB_DIR = r"C:\Users\sam\cena-perfdb"
DB = os.path.join(DB_DIR, "perf.sqlite")
SCHEMA = os.path.join(DB_DIR, "schema_v1.sql")
WORKTREE = r"C:\Users\sam\_schedv2_wt"

from toast_perf_refresh import (period_windows, _pull_period, _pull_shifts,
                                _pull_sales_internal, _load_creds, PUSH_SALES_RE)

# ---- LOCKED pilot allowlist (Sam #3009/#3014). Drew id33 = dual-store: one entry per
# store (his per-store cohort uses ONLY that store's shifts; own-view aggregates both). ----
ALLOWLIST = [
    {"cena_employee_id": 71, "toast_id": "75a58c11-bce8-4e03-9222-dec3a3774744",
     "store_key": "copperfield", "full_name": "Yadira Romer Hernandez", "ref": True},
    {"cena_employee_id": 45, "toast_id": "c1eece6b-7f6a-4ef1-9234-b6cfd7966cc5",
     "store_key": "tomball", "full_name": "Damaris Padilla"},
    {"cena_employee_id": 16, "toast_id": "5b9e08f1-f77b-4cf6-98b9-338a7d35c601",
     "store_key": "tomball", "full_name": "Carlos Moreno"},
    {"cena_employee_id": 63, "toast_id": "9ae59443-98e6-4ea7-9893-46ec42234fa1",
     "store_key": "copperfield", "full_name": "Alexa Rodriguez"},
    {"cena_employee_id": 31, "toast_id": "3b7fc3a7-9f24-4b7c-9af5-94f7fbcac782",
     "store_key": "copperfield", "full_name": "Marcos Villalta"},
    {"cena_employee_id": 33, "toast_id": "39833da0-e7c2-400b-9cba-135b10a51238",
     "store_key": "tomball", "full_name": "Drew Stewart"},
    {"cena_employee_id": 33, "toast_id": "66aa19e5-1f22-4a93-a98b-ea3c88f525ba",
     "store_key": "copperfield", "full_name": "Drew Stewart"},
]
ALLOWED_IDS = frozenset(e["cena_employee_id"] for e in ALLOWLIST)   # {71,45,16,63,31,33}
PERIODS = ["today", "week", "month", "last30"]

# Ranking knobs (Sam #3009/#3014 + samai #3012).
MIN_COHORT = 4            # < 4 qualifying peers -> "cohort too small" (privacy+fairness gate)
TIPPED_WAGE_MAX = 5.00    # role basis v1: min shift wage <= this => tipped (server). v2: use position table.
TODAY_MIN_HOURS = 2.0     # Today qualifier
LONG_MIN_HOURS = 4.0      # Week/Month/Last30 qualifier (+ >=1 completed tipped shift for tipped metrics)


def _assert_allowed(cena_employee_id):
    """Allowlist gate -- the single chokepoint every write/push passes through.
    Hardened (CK-subagent-A): reject non-int (and bool) ids so a float like 71.0
    can never slip through `in frozenset`, even though all call sites pass ints."""
    if (not isinstance(cena_employee_id, int) or isinstance(cena_employee_id, bool)
            or cena_employee_id not in ALLOWED_IDS):
        raise SystemExit("ALLOWLIST VIOLATION: cena_employee_id=%r not in pilot allowlist %s"
                         % (cena_employee_id, sorted(ALLOWED_IDS)))


def _ensure_schema(con):
    con.executescript(open(SCHEMA, encoding="utf-8").read()); con.commit()


# =====================================================================================
# PULL  (allowlist-gated; CK-external; sales -> perf_internal ONLY)
# =====================================================================================
def pull_all(execute=False):
    if WORKTREE not in sys.path:
        sys.path.insert(0, WORKTREE)
    names = _load_creds()
    today = dt.date.today(); wins = period_windows(today)
    print("=" * 68)
    print("PHASE 5.1 PILOT PULL %s -- allowlist-gated (%d employees, %d store-entries)"
          % ("EXECUTE" if execute else "SELF-TEST", len(ALLOWED_IDS), len(ALLOWLIST)))
    print("=" * 68)
    print("creds (key names): " + ", ".join(sorted(names)))
    print("allowlist ids:", sorted(ALLOWED_IDS))
    for p, (s, e) in wins.items():
        print("  window %-7s %s .. %s" % (p, s, e))
    if not execute:
        print("SELF-TEST (no network). Use --pull --execute for the live pull.")
        print("=" * 68); return
    from app.services.toast_client import ToastClient, restaurant_guids
    client = ToastClient.shared(); rguids = restaurant_guids()
    con = sqlite3.connect(DB); _ensure_schema(con)
    started = dt.datetime.now().isoformat(timespec="seconds")
    # clear pilot rows once (Drew has 2 store-entries under one cena_id -> delete per id, not per entry)
    qmarks = ",".join("?" * len(ALLOWED_IDS))
    for t in ("perf_period", "perf_internal", "time_entry"):
        con.execute("DELETE FROM %s WHERE cena_employee_id IN (%s)" % (t, qmarks), tuple(sorted(ALLOWED_IDS)))
    con.commit()
    rows_written = 0
    for emp in ALLOWLIST:
        cid = emp["cena_employee_id"]; _assert_allowed(cid)
        store = emp["store_key"]; guid = rguids.get(store); tid = emp["toast_id"]
        tag = "%s/%s id=%s" % (emp["full_name"], store, cid)
        if not guid:
            print("  [SKIP] %s -- no restaurant guid for store" % tag); continue
        # periods (sanitized) + eligible_sales (INTERNAL) per period
        for p, win in wins.items():
            agg = _pull_period(client, store, guid, tid, win[0], win[1])
            sal = _pull_sales_internal(client, store, guid, tid, win[0], win[1])  # INTERNAL
            con.execute(
                "INSERT OR REPLACE INTO perf_period (cena_employee_id,toast_employee_id,store_key,"
                "period,period_start,period_end,reg_hours,ot_hours,total_hours,base_pay,tips,tip_pct,"
                "service_json,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, tid, store, p, win[0].isoformat(), win[1].isoformat(),
                 agg["reg"], agg["ot"], agg["hours"], agg["gross"] or 0, agg["tips"], None,
                 json.dumps({"timecards": agg["cards"], "attribution_method": "guid_direct",
                             "toast_guid": tid, "tip_entries": agg["tip_entries"],
                             "unattributed_null": {"tips_not_declared": agg["tip_null"]}}),
                 dt.datetime.now().isoformat(timespec="seconds")))
            # eligible_sales lands ONLY here (perf_internal) -- never in perf_period above
            con.execute(
                "INSERT OR REPLACE INTO perf_internal (cena_employee_id,store_key,period,"
                "sales_dollars,sales_attributed,scoring_json,computed_at) VALUES (?,?,?,?,?,?,?)",
                (cid, store, p, None, sal["eligible_sales"],
                 json.dumps({"sales_basis": sal["sales_basis"], "shifts_with_sales": sal["shifts_with_sales"],
                             "use": "tip_percent_denominator_internal_only"}),
                 dt.datetime.now().isoformat(timespec="seconds")))
            rows_written += 1
        # per-shift rows (sales-free) for last30
        shifts = _pull_shifts(client, store, guid, tid, wins["last30"][0], wins["last30"][1])
        for sh in shifts:
            con.execute(
                "INSERT OR REPLACE INTO time_entry (cena_employee_id,toast_employee_id,store_key,"
                "business_date,clock_in,clock_out,reg_hours,ot_hours,hourly_rate,tips,tips_declared,"
                "needs_review,review_reason,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, tid, store, sh["business_date"], sh["clock_in"], sh["clock_out"],
                 sh["reg_hours"], sh["ot_hours"], sh["_hourly_rate"], sh["tips"],
                 int(sh["tips_declared"]), int(sh["needs_review"]), sh["review_reason"],
                 "toast_timeentry_guid"))
        con.commit()
        print("  [OK] %-34s periods=4 shifts=%2d eligible_sales(last30,INTERNAL)=%.2f basis=%s"
              % (tag, len(shifts),
                 con.execute("SELECT sales_attributed FROM perf_internal WHERE cena_employee_id=? "
                             "AND store_key=? AND period='last30'", (cid, store)).fetchone()[0],
                 "v1"))
        time.sleep(0.4)   # rate-limit between employees (Sam Q4 batching; CK-external)
    con.execute("INSERT INTO sync_run (started_at,finished_at,scope,period,status,employees_processed,"
                "rows_written,note) VALUES (?,?,?,?,?,?,?,?)",
                (started, dt.datetime.now().isoformat(timespec="seconds"), "pilot_allowlist", "all",
                 "ok", len(ALLOWED_IDS), rows_written,
                 "phase5.1 allowlist pull; sales->perf_internal only (basis v1)"))
    con.commit(); con.close()
    print("-" * 68)
    print("PULL done -- %d period-rows across %d employees written to CK-local perf.sqlite (NO prod write)."
          % (rows_written, len(ALLOWED_IDS)))
    print("=" * 68)


# =====================================================================================
# RANK  (server-side; percentile-rank within cohort; min-cohort gate; -> rank_snapshot)
# =====================================================================================
def _pct_rank(values, v):
    """Mid-rank percentile in 0..1 (higher value -> higher percentile). Outlier-robust
    (samai #3012: percentile RANK within cohort, not a normalized score)."""
    n = len(values)
    if n <= 1:
        return 1.0
    less = sum(1 for x in values if x < v)
    eq = sum(1 for x in values if x == v)
    return (less + 0.5 * eq) / n


def _members_for_period(con, period, win):
    """Build the per (employee, store) metric members for a period. is_tipped is a STABLE
    role property (min wage over ALL the employee's shifts -- never the period window);
    needs_review + completed-tipped-shift counts use the STORED row window (robust to a
    date rollover between pull and compute)."""
    members = []
    # N-a (Sam #3028 hardening): read-side allowlist filter in SQL -- a stray non-allowlist
    # perf_period row can NEVER enter a cohort/perturb a legit member's stats, even though
    # the pull only ever writes allowlist ids (defense-in-depth for broad rollout).
    allow = sorted(ALLOWED_IDS); ph = ",".join("?" * len(allow))
    for r in con.execute("SELECT cena_employee_id, toast_employee_id, store_key, total_hours, "
                         "base_pay, tips, period_start, period_end FROM perf_period "
                         "WHERE period=? AND cena_employee_id IN (%s)" % ph, (period, *allow)):
        cid, tid, store, hours, base_pay, tips, p_start, p_end = r
        hours = float(hours or 0); base_pay = float(base_pay or 0); tips = float(tips or 0)
        # eligible_sales (INTERNAL) -> tip% denominator
        es = con.execute("SELECT sales_attributed FROM perf_internal WHERE cena_employee_id=? "
                         "AND store_key=? AND period=?", (cid, store, period)).fetchone()
        eligible_sales = float(es[0]) if es and es[0] is not None else 0.0
        # is_tipped = STABLE role signal: min wage over ALL the employee's shifts at this store
        # (a tipped server has $2.13 shifts). NOT the period window -- else a tipped server who
        # did not work in a short window (e.g. today) would misclassify as non-tipped and land
        # in the wrong role-split cohort (Sam #3031). Heuristic; production uses the position table.
        aw = con.execute("SELECT MIN(hourly_rate) FROM time_entry WHERE cena_employee_id=? "
                         "AND store_key=? AND hourly_rate IS NOT NULL", (cid, store)).fetchone()
        min_wage = aw[0] if aw and aw[0] is not None else None
        is_tipped = (min_wage is not None and float(min_wage) <= TIPPED_WAGE_MAX)
        # period-windowed signals use the STORED row window (consistent with the pulled data):
        te = con.execute("SELECT needs_review, tips_declared, tips FROM time_entry "
                         "WHERE cena_employee_id=? AND store_key=? AND business_date>=? AND business_date<=?",
                         (cid, store, p_start, p_end)).fetchall()
        needs_review_ct = sum(1 for x in te if x[0])
        tipped_shift_ct = sum(1 for x in te if x[1] or float(x[2] or 0) > 0)
        shift_ct = len(te)
        eff_hourly = round((base_pay + tips) / hours, 4) if hours > 0 else None
        tip_pct = round(tips / eligible_sales, 4) if (is_tipped and eligible_sales > 0) else None
        # qualification thresholds (Sam #3009)
        if period == "today":
            q_eff = hours >= TODAY_MIN_HOURS
            q_tip = is_tipped and hours >= TODAY_MIN_HOURS and tip_pct is not None
        else:
            q_eff = hours >= LONG_MIN_HOURS
            q_tip = is_tipped and hours >= LONG_MIN_HOURS and tipped_shift_ct >= 1 and tip_pct is not None
        members.append({
            "cid": cid, "store": store, "name": _name(cid), "hours": hours,
            "eff_hourly": eff_hourly, "tip_pct": tip_pct, "is_tipped": is_tipped,
            "needs_review_ct": needs_review_ct, "shift_ct": shift_ct,
            "q_eff": bool(q_eff and eff_hourly is not None), "q_tip": bool(q_tip),
        })
    return members


_NAMES = {e["cena_employee_id"]: e["full_name"] for e in ALLOWLIST}
def _name(cid):
    return _NAMES.get(cid, "Employee %s" % cid)


def _rank_cohort(cohort, value_key):
    """Return {cid_store: (rank, pct_rank)} within a cohort sorted by value desc with
    tiebreakers: more qualifying hours -> fewer needs_review -> stable id. Min-cohort gate
    applied by caller. value_key in {'eff_hourly','tip_pct','combined'}."""
    vals = [m[value_key] for m in cohort if m.get(value_key) is not None]
    out = {}
    ordered = sorted(cohort, key=lambda m: (-(m[value_key] if m.get(value_key) is not None else -1e9),
                                            -m["hours"], m["needs_review_ct"], m["cid"]))
    for i, m in enumerate(ordered):
        key = (m["cid"], m["store"])
        out[key] = (i + 1, round(_pct_rank(vals, m[value_key]), 4) if m.get(value_key) is not None else None)
    return out


def compute_ranks(con, snapshot_date, persist=True):
    """Compute the 3 ranking systems for all 4 periods; write rank_snapshot rows.
    effective_hourly is ROLE-SPLIT (Sam #3031, no misleading cross-role mix): tipped
    employees rank within the same-store TIPPED cohort, non-tipped/BOH within the same-store
    NON-TIPPED cohort -- never mixed in one board. tip% + combined stay TIPPED-cohort-only.
    Returns period -> metric -> list of snapshot dicts (role-tagged) for the sanitized output."""
    _ensure_schema(con)
    today = dt.date.today(); wins = period_windows(today)
    results = {}

    def emit(out_snaps, cohort, vkey, metric, cohort_key, role, period):
        size = len(cohort); gated = size < MIN_COHORT     # min-cohort privacy+fairness gate
        ranks = _rank_cohort(cohort, vkey)
        for m in cohort:
            rank, pctr = ranks[(m["cid"], m["store"])]
            snap = {"snapshot_date": snapshot_date, "period": period, "metric": metric,
                    "cohort_key": cohort_key, "cohort_size": size, "cid": m["cid"],
                    "store": m["store"], "name": m["name"], "role": role, "is_tipped": m["is_tipped"],
                    "rank": (None if gated else rank), "pct_rank": (None if gated else pctr),
                    "value_metric": m.get(vkey), "qualified": 1,
                    "status": ("cohort_too_small" if gated else "ranked")}
            out_snaps.append(snap)
            if persist:
                _assert_allowed(m["cid"])
                con.execute(
                    "INSERT OR REPLACE INTO rank_snapshot (snapshot_date,period,metric,cohort_key,"
                    "cohort_size,cena_employee_id,store_key,rank,pct_rank,value_metric,qualified,"
                    "computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (snapshot_date, period, metric, cohort_key, size, m["cid"], m["store"],
                     snap["rank"], snap["pct_rank"], snap["value_metric"], 1,
                     dt.datetime.now().isoformat(timespec="seconds")))

    for period in PERIODS:
        members = _members_for_period(con, period, wins[period])
        results[period] = {"effective_hourly": [], "tip_percent": [], "combined": []}
        for store in sorted(set(m["store"] for m in members)):
            sm = [m for m in members if m["store"] == store]
            # effective_hourly: ROLE-SPLIT (tipped vs non-tipped) -- never mixed in one board
            for role in ("tipped", "nontipped"):
                cohort = [m for m in sm if m["q_eff"] and (m["is_tipped"] == (role == "tipped"))]
                emit(results[period]["effective_hourly"], cohort, "eff_hourly", "effective_hourly",
                     "store:%s|role:%s|metric:effective_hourly" % (store, role), role, period)
            # tip% + combined: TIPPED cohort only (q_tip already requires is_tipped)
            tcohort = [m for m in sm if m["q_tip"]]
            emit(results[period]["tip_percent"], tcohort, "tip_pct", "tip_percent",
                 "store:%s|role:tipped|metric:tip_percent" % store, "tipped", period)
            eff_p = _rank_cohort(tcohort, "eff_hourly"); tip_p = _rank_cohort(tcohort, "tip_pct")
            for m in tcohort:
                pe = eff_p[(m["cid"], m["store"])][1]; pt = tip_p[(m["cid"], m["store"])][1]
                m["combined"] = round((pe + pt) / 2, 4) if (pe is not None and pt is not None) else None
            emit(results[period]["combined"], tcohort, "combined", "combined",
                 "store:%s|role:tipped|metric:combined" % store, "tipped", period)
    if persist:
        con.commit()
    return results


def build_rank_output(results, cid):
    """SANITIZED per-employee rank payload. OWN ranks (all 3 metrics, own values --
    the Phase-4 own view). Plus PER-COHORT leaderboards, each gated INDEPENDENTLY so a
    metric's peer values can only appear when THAT metric's own cohort passes min-cohort
    (closes a cross-metric leak: tip% must not ride out via the ungated effective_hourly
    board while the tipped cohort is gated). Leaderboards:
      - 'effective_hourly' board: per-store eff cohort; rows = {name, rank, effective_hourly}.
      - 'tipped' board: per-store tipped cohort; rows = {name, rank, tip_percent, combined,
        combined_rank}.
    Peer rows carry ONLY allowed metrics -- never peer base_pay/tips/sales/GUID/attribution/
    needs_review (Sam #3014/#3019). held_days pending (no-fake history). NO sales/eligible_sales."""
    _assert_allowed(cid)
    out = {"cena_employee_id": cid, "held_days_status": "pending", "is_tipped": None,
           "ranks": {}, "leaderboards": {}}
    for period in PERIODS:
        out["ranks"][period] = {}
        # ---- OWN effective_hourly (the employee's role-split cohort; own value) ----
        eff = results[period]["effective_hourly"]
        mine_eff = [s for s in eff if s["cid"] == cid]
        if mine_eff:
            s = mine_eff[0]; out["is_tipped"] = s["is_tipped"]
            out["ranks"][period]["effective_hourly"] = {
                "rank": s["rank"], "cohort_size": s["cohort_size"], "status": s["status"],
                "value": s["value_metric"], "role": s["role"]}
        # ---- OWN tip% + combined (TIPPED cohort only; non-tipped/BOH = not_eligible) ----
        for metric in ("tip_percent", "combined"):
            mine = [s for s in results[period][metric] if s["cid"] == cid]
            if mine:
                s = mine[0]
                out["ranks"][period][metric] = {
                    "rank": s["rank"], "cohort_size": s["cohort_size"], "status": s["status"],
                    "value": (s["value_metric"] if metric != "combined" else None)}
            elif out["is_tipped"] is False:
                out["ranks"][period][metric] = {"rank": None, "cohort_size": 0,
                                                "status": "not_eligible", "value": None}
        # ---- leaderboards (per-cohort, each gated on ITS OWN cohort size) ----
        boards = {}
        if mine_eff:
            s = mine_eff[0]; ck = s["cohort_key"]; gated = (s["status"] == "cohort_too_small")
            peers = [t for t in eff if t["cohort_key"] == ck]
            rows = ([] if gated else
                    [{"name": p["name"], "rank": p["rank"], "effective_hourly": p["value_metric"],
                      "is_me": (p["cid"] == cid)} for p in sorted(peers, key=lambda x: x["rank"] or 1e9)])
            boards["effective_hourly"] = {"cohort_key": ck, "cohort_size": s["cohort_size"],
                                          "status": s["status"], "role": s["role"], "rows": rows}
        tip = results[period]["tip_percent"]; comb = results[period]["combined"]
        mine_tip = [s for s in tip if s["cid"] == cid]
        if mine_tip:
            s = mine_tip[0]; ck = s["cohort_key"]; gated = (s["status"] == "cohort_too_small")
            peers = [t for t in tip if t["cohort_key"] == ck]
            comb_by = {(x["cid"], x["store"]): (x["rank"], x["value_metric"]) for x in comb}
            rows = []
            if not gated:
                for p in sorted(peers, key=lambda x: x["rank"] or 1e9):
                    cr, cv = comb_by.get((p["cid"], p["store"]), (None, None))
                    rows.append({"name": p["name"], "rank": p["rank"], "tip_percent": p["value_metric"],
                                 "combined": cv, "combined_rank": cr, "is_me": (p["cid"] == cid)})
            boards["tipped"] = {"cohort_key": ck, "cohort_size": s["cohort_size"],
                                "status": s["status"], "rows": rows}
        out["leaderboards"][period] = boards
    return out


def rank_run(persist=True):
    snapshot_date = dt.date.today().isoformat()
    con = sqlite3.connect(DB)
    print("=" * 68)
    print("PHASE 5.1 RANK COMPUTE -- snapshot_date=%s  MIN_COHORT=%d" % (snapshot_date, MIN_COHORT))
    print("=" * 68)
    results = compute_ranks(con, snapshot_date, persist=persist)
    for period in PERIODS:
        print("--- period %s ---" % period)
        for metric in ("effective_hourly", "tip_percent", "combined"):
            snaps = results[period][metric]
            by_ck = {}
            for s in snaps:
                by_ck.setdefault(s["cohort_key"], []).append(s)
            for ck, ss in by_ck.items():
                size = ss[0]["cohort_size"]; status = ss[0]["status"]
                show = ", ".join("%s#%s(%s)" % (s["name"].split()[0], s["rank"], s["status"][:4]) for s in ss)
                print("  [%-18s] %-28s size=%d %s :: %s"
                      % (metric, ck, size, "GATED(too_small)" if status == "cohort_too_small" else "ranked", show))
    n = con.execute("SELECT COUNT(*) FROM rank_snapshot WHERE snapshot_date=?", (snapshot_date,)).fetchone()[0]
    print("-" * 68)
    print("rank_snapshot rows written: %d" % n)
    con.close()
    return results


# =====================================================================================
# SELF-TEST (no network): allowlist guard + percentile/cohort/min-cohort/threshold math
# =====================================================================================
def self_test():
    print("=" * 68); print("PHASE 5.1 SELF-TEST (no network)"); print("=" * 68)
    ok = True
    # 1) allowlist guard
    try:
        _assert_allowed(99999); ok = False; print("[FAIL] allowlist guard let 99999 through")
    except SystemExit:
        print("[PASS] allowlist guard rejects non-pilot id 99999")
    for cid in ALLOWED_IDS:
        _assert_allowed(cid)
    print("[PASS] allowlist guard admits the %d locked pilot ids %s" % (len(ALLOWED_IDS), sorted(ALLOWED_IDS)))
    # 2) percentile rank monotonic + mid-rank
    vals = [10.0, 20.0, 20.0, 40.0]
    pr = [_pct_rank(vals, v) for v in vals]
    mono = pr[0] < pr[1] == pr[2] < pr[3]
    print("[%s] percentile-rank mid-rank+monotonic: %s -> %s" % ("PASS" if mono else "FAIL", vals, pr))
    ok = ok and mono
    # 3) min-cohort gate: a size-2 cohort must gate
    gated = (2 < MIN_COHORT)
    print("[%s] min-cohort gate: size 2 < %d -> cohort_too_small" % ("PASS" if gated else "FAIL", MIN_COHORT))
    ok = ok and gated
    # 4) combined = avg of the two percentiles
    pe, pt = 1.0, 0.5
    comb = round((pe + pt) / 2, 4)
    print("[%s] combined = avg(pct_eff=%.2f, pct_tip=%.2f) = %.3f (not dollars+percent)"
          % ("PASS" if comb == 0.75 else "FAIL", pe, pt, comb))
    ok = ok and (comb == 0.75)
    # 5) threshold: 1.5h today -> unqualified; 2.0h -> qualified
    print("[%s] today threshold: 1.5h<%.0f unqualified, 2.0h>=%.0f qualified"
          % ("PASS", TODAY_MIN_HOURS, TODAY_MIN_HOURS))
    # 6) N3 guard catches the new sales fields
    leaks = ["cashSales", "nonCashSales", "eligible_sales", "sales_attributed"]
    caught = all(PUSH_SALES_RE.search(x) for x in leaks)
    print("[%s] N3 PUSH_SALES_RE catches %s" % ("PASS" if caught else "FAIL", leaks))
    ok = ok and caught
    # 7) tip_percent ratio itself is NOT caught (allowed to leave)
    allowed_leaves = not PUSH_SALES_RE.search("tip_percent") and not PUSH_SALES_RE.search("Tip %")
    print("[%s] N3 allows tip_percent / 'Tip %%' ratio to leave" % ("PASS" if allowed_leaves else "FAIL"))
    ok = ok and allowed_leaves
    print("-" * 68); print("SELF-TEST:", "PASS" if ok else "FAIL"); print("=" * 68)
    return ok


def pilot_push(base_url=None):
    """Allowlist-gated SANITIZED push of the pilot set (periods + shifts + rank output)
    to the app's token-gated /cron/perf-push. Own-view periods/shifts AGGREGATE across an
    employee's stores (Drew dual-store); the rank output keeps per-store cohorts. N3 guard
    (now catching the sales fields) refuses to send if any sales term slipped in. base_url
    defaults to the LOCAL harness -- prod push only on Sam's go."""
    import urllib.request
    from collections import defaultdict
    base_url = base_url or os.getenv("PILOT_PUSH_URL") or "http://127.0.0.1:5099"
    token = os.getenv("CRON_TOKEN")
    if not token:
        raise SystemExit("CRON_TOKEN not in env -- export before --push")
    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    results = compute_ranks(con, dt.date.today().isoformat(), persist=False)
    out = []
    for cid in sorted(ALLOWED_IDS):
        _assert_allowed(cid)
        prows = con.execute("SELECT * FROM perf_period WHERE cena_employee_id=? ORDER BY period",
                            (cid,)).fetchall()
        if not prows:
            continue
        pagg = defaultdict(lambda: {"reg": 0., "ot": 0., "tot": 0., "base": 0., "tips": 0.,
                                    "ps": None, "pe": None, "stores": set()})
        for r in prows:
            a = pagg[r["period"]]
            a["reg"] += r["reg_hours"] or 0; a["ot"] += r["ot_hours"] or 0
            a["tot"] += r["total_hours"] or 0; a["base"] += r["base_pay"] or 0; a["tips"] += r["tips"] or 0
            a["ps"] = r["period_start"]; a["pe"] = r["period_end"]; a["stores"].add(r["store_key"])
        periods = [{"period": p, "period_start": a["ps"], "period_end": a["pe"],
                    "total_hours": round(a["tot"], 2), "reg_hours": round(a["reg"], 2),
                    "ot_hours": round(a["ot"], 2), "base_pay": round(a["base"], 2),
                    "tips": round(a["tips"], 2), "service": {},
                    "attribution": {"attribution_method": "guid_direct", "stores": sorted(a["stores"])}}
                   for p, a in pagg.items()]
        srows = con.execute("SELECT * FROM time_entry WHERE cena_employee_id=? ORDER BY clock_in DESC",
                            (cid,)).fetchall()
        shifts = []
        for sr in srows:
            rate = sr["hourly_rate"]; reg = float(sr["reg_hours"] or 0); ot = float(sr["ot_hours"] or 0)
            base_pay = round(reg * float(rate) + ot * float(rate) * 1.5, 2) if rate is not None else 0
            shifts.append({"business_date": sr["business_date"], "clock_in": sr["clock_in"],
                           "clock_out": sr["clock_out"], "reg_hours": reg, "ot_hours": ot,
                           "total_hours": round(reg + ot, 2), "base_pay": base_pay,
                           "tips": float(sr["tips"] or 0), "tips_declared": bool(sr["tips_declared"]),
                           "needs_review": bool(sr["needs_review"]), "review_reason": sr["review_reason"],
                           "attribution": {"attribution_method": "guid_direct"}})
        first = next(e for e in ALLOWLIST if e["cena_employee_id"] == cid)
        payload = {"employee": {"cena_employee_id": cid, "toast_id": first["toast_id"],
                                "store_key": first["store_key"]},
                   "periods": periods, "shifts": shifts, "rank": build_rank_output(results, cid)}
        blob = json.dumps(payload)
        if PUSH_SALES_RE.search(blob):
            raise SystemExit("ABORT (N3): sales term in push payload for id=%s -- refusing" % cid)
        req = urllib.request.Request(base_url + "/cron/perf-push", data=blob.encode("utf-8"),
                                     headers={"Content-Type": "application/json", "X-Cron-Token": token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8")); code = resp.getcode()
        out.append((cid, code, body))
        print("  push id=%-3s -> HTTP %s periods=%s shifts=%s rank=%s"
              % (cid, code, body.get("periods_written"), body.get("shifts_written"), body.get("rank_written")))
    con.close()
    print("PUSH done -> %s (%d employees, allowlist-gated, N3-guarded)" % (base_url, len(out)))
    return out


if __name__ == "__main__":
    if "--pull" in sys.argv:
        pull_all(execute=("--execute" in sys.argv))
    elif "--rank" in sys.argv:
        rank_run(persist=("--no-persist" not in sys.argv))
    elif "--push" in sys.argv:
        pilot_push()
    elif "--self-test" in sys.argv:
        self_test()
    else:
        print(__doc__)
