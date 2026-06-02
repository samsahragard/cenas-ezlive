"""T108 REAL-DATA proof capture (Sam, 2026-06-02).

Produces REAL-DATA proof payloads for the T108 employee performance UI for ONE
real tipped employee (id 71, Yadira Romer Hernandez, copperfield) and ONE real
BOH / non-tipped employee (id 16, Carlos Moreno, tomball).

LIGHTEST CORRECT PATH (no Toast pull, no app edit, no prod push, never reads the
perf_internal SALES table from any code in THIS file):
  1. Compute the REAL rank_json WITH next_best by running the engine's existing
     no-pull rank-compute against the STORED CK perf DB:
       pilot_phase5.set_active_set(load_eligible())          # eligible set
       results = pilot_phase5.compute_ranks(con, date, persist=False)   # reads perf.sqlite ONLY
       rank_json = pilot_phase5.build_rank_output(results, cid)         # emits ranks[period].next_best
     (compute_ranks reads perf_period / time_entry / perf_internal INTERNALLY for the
      tip% denominator, but build_rank_output's sanitized output carries NO sales/$;
      THIS file never SELECTs perf_internal.)
  2. Seed a TEMP sqlite app DB (t108_harness pattern) with each employee's real
     Employee + CenaToastLink + real perf_period rows (as PerfPeriodCache) + real
     time_entry rows (as PerfShiftCache) + their real rank_json (as PerfRankCache).
  3. Drive the app via test_client with s['employee_id']=ID; s['auth_ok']=True and
     GET BOTH self-view endpoints, because the two required properties live in two
     different real endpoints:
       - /employee/performance-center  -> the T108 role-aware detail payload. Proves
         the BOH money block OMITS every tip key SERVER-SIDE (route gates them behind
         `if is_tipped:`). This endpoint does NOT echo next_best.
       - /employee/my-performance      -> returns resp['ranking'] = the sanitized
         rank_json VERBATIM, so ranks[period].next_best is present here (this is the
         endpoint employee_dashboard.html reads for the next-best nudge).
     The saved proof file for each employee bundles BOTH real responses so every
     required assertion is proven on real server output.

NO Toast pull. NO app file edit. NO prod push. Temp DB only (deleted on exit).
ASCII-safe.

Run:  cd /c/Users/sam/_schedv2_wt && python perfdb/t108_real_proof.py
"""
import os, sys, json, re, sqlite3, tempfile, datetime as dt

WT = r"C:\Users\sam\_schedv2_wt"
PERFDB_DIR = os.path.join(WT, "perfdb")
CK_DB = r"C:\Users\sam\cena-perfdb\perf.sqlite"
PROOF = os.path.join(PERFDB_DIR, "proof")
TIPPED_ID = 71   # Yadira Romer Hernandez, copperfield (is_tipped True)
BOH_ID = 16      # Carlos Moreno, tomball (is_tipped False)

sys.path.insert(0, WT)
sys.path.insert(0, PERFDB_DIR)

# --- env BEFORE importing app (app/db.py reads DATABASE_URL at import time) ---
fd, dbpath = tempfile.mkstemp(suffix="_t108_real.sqlite"); os.close(fd)
os.environ["DATABASE_URL"] = "sqlite:///" + dbpath.replace("\\", "/")
os.environ["CRON_TOKEN"] = "testtoken123"
os.environ["SECRET_KEY"] = "test-secret-t108-real"
os.environ["ALLOW_DEV_SECRET"] = "1"

import pilot_phase5 as P
from app import create_app
from app.db import engine, SessionLocal
from app.models import (Employee, CenaToastLink, PerfPeriodCache,
                        PerfShiftCache, PerfRankCache)

app = create_app()   # Base.metadata.create_all on the temp DB

# Dual-grep blocklists. SALES per the task spec; INTERNAL = GUID/attribution/internal.
SALES = re.compile(
    r"cc_subtotal|cash_amount|net_sales|check_total|revenue|gross|eligible_sales|"
    r"cashSales|nonCashSales|\bsales\b", re.I)
INTERNAL = re.compile(
    r"attribution_method|toast_guid|toast_id|perf_internal|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", re.I)


def compute_real_rank_json():
    """Compute REAL rank_json (WITH next_best) for both ids from the STORED CK DB.
    No Toast pull. Returns (names, {cid: rank_json})."""
    P.set_active_set(P.load_eligible())
    names = {e["cena_employee_id"]: e["full_name"] for e in P.ALLOWLIST}
    con = sqlite3.connect(P.DB)   # P.DB == the CK perf.sqlite (read-only use here)
    try:
        results = P.compute_ranks(con, dt.date.today().isoformat(), persist=False)
    finally:
        con.close()
    return names, {cid: P.build_rank_output(results, cid) for cid in (TIPPED_ID, BOH_ID)}


def load_real_periods(cid):
    """Real perf_period rows for cid from the CK DB (sanitized cols only -- NO sales).
    THIS query never touches perf_internal."""
    con = sqlite3.connect(CK_DB); con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT cena_employee_id,toast_employee_id,store_key,period,period_start,"
            "period_end,total_hours,reg_hours,ot_hours,base_pay,tips "
            "FROM perf_period WHERE cena_employee_id=?", (cid,)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def load_real_shifts(cid):
    """Real time_entry (per-shift) rows for cid from the CK DB. NO perf_internal."""
    con = sqlite3.connect(CK_DB); con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT toast_employee_id,store_key,business_date,clock_in,clock_out,"
            "reg_hours,ot_hours,hourly_rate,tips,tips_declared,needs_review,review_reason "
            "FROM time_entry WHERE cena_employee_id=? ORDER BY clock_in", (cid,)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def seed(names, rank_by_cid):
    db = SessionLocal()
    try:
        for cid in (TIPPED_ID, BOH_ID):
            periods = load_real_periods(cid)
            shifts = load_real_shifts(cid)
            tid = periods[0]["toast_employee_id"] if periods else None
            store = periods[0]["store_key"] if periods else None
            db.add(Employee(id=cid, full_name=names[cid], active=True))
            db.add(CenaToastLink(cena_employee_id=cid, store_key=store,
                                 toast_id=tid, toast_name=names[cid]))
            for r in periods:
                db.add(PerfPeriodCache(
                    cena_employee_id=cid, toast_id=r["toast_employee_id"],
                    store_key=r["store_key"], period=r["period"],
                    period_start=r["period_start"], period_end=r["period_end"],
                    total_hours=r["total_hours"] or 0.0, reg_hours=r["reg_hours"] or 0.0,
                    ot_hours=r["ot_hours"] or 0.0, base_pay=r["base_pay"] or 0.0,
                    tips=r["tips"] or 0.0, service_json={},
                    attribution_json={"attribution_method": "guid_direct",
                                      "toast_guid": r["toast_employee_id"]}))
            for s in shifts:
                rate = s["hourly_rate"]
                reg = float(s["reg_hours"] or 0); ot = float(s["ot_hours"] or 0)
                base_pay = (round(reg * float(rate) + ot * float(rate) * 1.5, 2)
                            if rate is not None else 0.0)
                db.add(PerfShiftCache(
                    cena_employee_id=cid, toast_id=s["toast_employee_id"],
                    store_key=s["store_key"], business_date=s["business_date"],
                    clock_in=s["clock_in"], clock_out=s["clock_out"],
                    reg_hours=reg, ot_hours=ot, total_hours=round(reg + ot, 2),
                    base_pay=base_pay, tips=float(s["tips"] or 0),
                    tips_declared=bool(s["tips_declared"]),
                    needs_review=bool(s["needs_review"]),
                    review_reason=s["review_reason"],
                    attribution_json={"attribution_method": "guid_direct"}))
            db.add(PerfRankCache(cena_employee_id=cid, rank_json=rank_by_cid[cid],
                                 computed_at=dt.datetime.now().isoformat(timespec="seconds")))
        db.commit()
    finally:
        db.close()


def as_session(client, emp_id):
    with client.session_transaction() as s:
        s["employee_id"] = emp_id
        s["auth_ok"] = True   # clears the global site gate


def capture(emp_id):
    """GET both real self-view endpoints for emp_id; return (center_json, myperf_json,
    combined_body_text)."""
    c = app.test_client(); as_session(c, emp_id)
    r_center = c.get("/employee/performance-center")
    r_myperf = c.get("/employee/my-performance")
    center = r_center.get_json(silent=True) or {}
    myperf = r_myperf.get_json(silent=True) or {}
    body = r_center.get_data(as_text=True) + "\n" + r_myperf.get_data(as_text=True)
    return center, myperf, body, r_center.status_code, r_myperf.status_code


def grep(body):
    return SALES.findall(body or ""), INTERNAL.findall(body or "")


def main():
    os.makedirs(PROOF, exist_ok=True)
    names, rank_by_cid = compute_real_rank_json()
    seed(names, rank_by_cid)

    checks = []   # (name, passed, detail_lines)

    # ---------- TIPPED (id 71) ----------
    t_center, t_myperf, t_body, t_sc, t_sm = capture(TIPPED_ID)
    t_money = (((t_center.get("periods") or {}).get("last30") or {}).get("money") or {})
    t_ranking = t_myperf.get("ranking") or {}
    t_nb = (((t_ranking.get("ranks") or {}).get("last30") or {}).get("next_best"))
    proof_tipped = {
        "_meta": {
            "kind": "REAL_DATA_PROOF",
            "role": "tipped",
            "cena_employee_id": TIPPED_ID,
            "full_name": names[TIPPED_ID],
            "rank_json_source": "pilot_phase5.compute_ranks(stored CK perf.sqlite, persist=False)"
                                " + build_rank_output  (NO Toast pull)",
            "performance_center_status": t_sc,
            "my_performance_status": t_sm,
        },
        "performance_center": t_center,   # role-aware detail payload (money tip keys present)
        "my_performance": t_myperf,       # carries ranks[period].next_best verbatim
    }

    det = []
    p_t_money = ("tips" in t_money and "tip_pct" in t_money and "tips_per_hour" in t_money)
    det.append("    /performance-center: status=%s is_tipped=%s last30.money keys=%s"
               % (t_sc, t_center.get("is_tipped"), sorted(t_money.keys())))
    p_t_is = (t_center.get("is_tipped") is True)
    p_t_nb = (t_nb is not None)
    det.append("    /my-performance: ranks[last30].next_best=%s" % json.dumps(t_nb))
    checks.append(("TIPPED is_tipped true + last30.money HAS tips/tip_pct/tips_per_hour + next_best present",
                   (t_sc == 200 and t_sm == 200 and p_t_is and p_t_money and p_t_nb), det))

    # ---------- BOH (id 16) ----------
    b_center, b_myperf, b_body, b_sc, b_sm = capture(BOH_ID)
    b_money = (((b_center.get("periods") or {}).get("last30") or {}).get("money") or {})
    b_ranking = b_myperf.get("ranking") or {}
    b_nb = (((b_ranking.get("ranks") or {}).get("last30") or {}).get("next_best"))
    b_money_keys = sorted(b_money.keys())
    tip_keys_present = [k for k in ("tips", "tip_pct", "tips_per_hour") if k in b_money]
    proof_boh = {
        "_meta": {
            "kind": "REAL_DATA_PROOF",
            "role": "boh_non_tipped",
            "cena_employee_id": BOH_ID,
            "full_name": names[BOH_ID],
            "rank_json_source": "pilot_phase5.compute_ranks(stored CK perf.sqlite, persist=False)"
                                " + build_rank_output  (NO Toast pull)",
            "performance_center_status": b_sc,
            "my_performance_status": b_sm,
            "last30_money_keys": b_money_keys,
            "tip_keys_present_in_money": tip_keys_present,
        },
        "performance_center": b_center,   # role-aware: tip keys OMITTED server-side
        "my_performance": b_myperf,       # carries ranks[period].next_best verbatim
    }

    det = []
    p_b_is = (b_center.get("is_tipped") is False)
    p_b_no_tip = (not tip_keys_present)
    p_b_nb = (b_nb is not None)
    det.append("    /performance-center: status=%s is_tipped=%s last30.money keys=%s tip_keys_present=%s"
               % (b_sc, b_center.get("is_tipped"), b_money_keys, tip_keys_present or "NONE"))
    det.append("    /my-performance: ranks[last30].next_best=%s" % json.dumps(b_nb))
    checks.append(("BOH is_tipped false + last30.money has NO tips/tip_pct/tips_per_hour + next_best present",
                   (b_sc == 200 and b_sm == 200 and p_b_is and p_b_no_tip and p_b_nb), det))

    # ---------- dual-grep BOTH bundled proof payloads (serialized exactly as saved) ----------
    det = []
    g_ok = True
    for label, payload in (("TIPPED bundle", proof_tipped), ("BOH bundle", proof_boh)):
        blob = json.dumps(payload)
        s_hits, i_hits = grep(blob)
        det.append("    [%s] SALES=%s INTERNAL/GUID=%s"
                   % (label, s_hits or "NONE", i_hits or "NONE"))
        g_ok = g_ok and (not s_hits) and (not i_hits)
    checks.append(("Dual-grep ZERO sales + ZERO GUID/internal in BOTH bundled proof payloads", g_ok, det))

    # ---------- write proof files ----------
    det = []
    pth_t = os.path.join(PROOF, "t108_real_tipped.json")
    pth_b = os.path.join(PROOF, "t108_real_boh.json")
    try:
        with open(pth_t, "w", encoding="utf-8") as f:
            json.dump(proof_tipped, f, indent=2, sort_keys=True)
        with open(pth_b, "w", encoding="utf-8") as f:
            json.dump(proof_boh, f, indent=2, sort_keys=True)
        wp = (os.path.getsize(pth_t) > 0 and os.path.getsize(pth_b) > 0)
        det.append("    wrote %s (%d bytes)" % (pth_t, os.path.getsize(pth_t)))
        det.append("    wrote %s (%d bytes)" % (pth_b, os.path.getsize(pth_b)))
    except Exception as e:   # noqa: BLE001
        wp = False
        det.append("    EXCEPTION writing proof: %s" % e)
    checks.append(("Proof JSON written for tipped + BOH", wp, det))

    # ---------- report ----------
    print("=" * 78)
    print("T108 REAL-DATA performance proof  (temp DB: %s)" % dbpath)
    print("=" * 78)
    print("Employees (REAL, from CK perf.sqlite):")
    print("  TIPPED : id=%s  %s" % (TIPPED_ID, names[TIPPED_ID]))
    print("  BOH    : id=%s  %s" % (BOH_ID, names[BOH_ID]))
    print("-" * 78)
    print("BOH last30.money key list (server-side): %s" % b_money_keys)
    print("BOH tip keys present in money           : %s" % (tip_keys_present or "NONE (correct)"))
    print("-" * 78)
    print("TIPPED ranks[last30].next_best: %s" % json.dumps(t_nb))
    print("BOH    ranks[last30].next_best: %s" % json.dumps(b_nb))
    print("=" * 78)
    overall = True
    for i, (name, passed, det_lines) in enumerate(checks, start=1):
        print("CHECK %d: %s -- %s" % (i, "PASS" if passed else "FAIL", name))
        for line in det_lines:
            print(line)
        overall = overall and passed
    print("=" * 78)
    print("OVERALL: %s" % ("PASS" if overall else "FAIL"))
    print("=" * 78)
    return overall


if __name__ == "__main__":
    ok = False
    try:
        ok = main()
    finally:
        try:
            engine.dispose(); os.remove(dbpath)
        except Exception:
            pass
    sys.exit(0 if ok else 1)
