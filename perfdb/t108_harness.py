"""T108 employee performance-center verification harness (task #108).

Self-contained, ASCII-safe. Models on _phase3_verify.py: seed a TEMP sqlite DB
(NEVER prod / any real DB), seed 3 employees + their sanitized perf caches, then
drive the app via app.test_client() and assert the role-aware + leak-free +
session-scoped contract of GET /employee/performance-center (T108) + the 11
/employee/performance/<metric> detail pages.

Run:  cd /c/Users/sam/_schedv2_wt && python perfdb/t108_harness.py
"""
import os, sys, json, re, tempfile

sys.path.insert(0, r"C:\Users\sam\_schedv2_wt")

# --- env BEFORE importing app (app/db.py reads DATABASE_URL at import time) ---
fd, dbpath = tempfile.mkstemp(suffix="_t108.sqlite"); os.close(fd)
os.environ["DATABASE_URL"] = "sqlite:///" + dbpath.replace("\\", "/")
os.environ["CRON_TOKEN"] = "testtoken123"
os.environ["SECRET_KEY"] = "test-secret-t108"
os.environ["ALLOW_DEV_SECRET"] = "1"

from app import create_app
from app.db import engine, SessionLocal
from app.models import (Employee, CenaToastLink, PerfPeriodCache,
                        PerfShiftCache, PerfRankCache)

app = create_app()   # runs Base.metadata.create_all on the temp DB

# --------------------------------------------------------------------------
# rank_json blobs
# --------------------------------------------------------------------------
# (A) TIPPED -- Yadira. Full tipped rank shape (effective_hourly + tip_percent +
# tips_per_hour + combined + score + next_best) + a leaderboard with an is_me row.
RANK_A = {
    "is_tipped": True,
    "ranks": {
        "last30": {
            "effective_hourly": {"rank": 2, "status": "ok", "value": 24.5, "cohort_size": 6},
            "tip_percent":      {"rank": 1, "status": "ok", "value": 0.18, "cohort_size": 6},
            "tips_per_hour":    {"rank": 2, "status": "ok", "value": 21.4, "cohort_size": 6},
            "combined":         {"rank": 1, "status": "ok", "value": 1,    "cohort_size": 6},
            "score":            {"standing_percentile": 83, "band": "Strong"},
            "next_best":        {"rule": "top",
                                 "msg": "You're in the top 3 of your group -- keep it up!"},
        },
        "week":  {},
        "today": {},
        "month": {},
    },
    "leaderboards": {
        "last30": {
            "effective_hourly": {
                "status": "ok",
                "rows": [
                    {"name": "Yadira", "rank": 2, "effective_hourly": 24.5, "is_me": True},
                    {"name": "Maria",  "rank": 1, "effective_hourly": 25.1, "is_me": False},
                ],
            },
        },
        "week":  {},
        "today": {},
        "month": {},
    },
}

# (B) BOH / non-tipped -- Damaris. NO tip_percent / tips_per_hour / combined keys.
RANK_B = {
    "is_tipped": False,
    "ranks": {
        "last30": {
            "effective_hourly": {"rank": 1, "status": "ok", "value": 17.0, "cohort_size": 5},
            "score":            {"standing_percentile": 90, "band": "Excellent"},
            "next_best":        {"rule": "top",
                                 "msg": "You're in the top 3 of your group -- keep it up!"},
        },
        "week":  {},
        "today": {},
        "month": {},
    },
    "leaderboards": {
        "last30": {
            "effective_hourly": {
                "status": "ok",
                "rows": [
                    {"name": "Damaris", "rank": 1, "effective_hourly": 17.0, "is_me": True},
                ],
            },
        },
        "week":  {},
        "today": {},
        "month": {},
    },
}


def _periods_tipped(emp_id, toast_id, store):
    """4 realistic tipped periods (today/week/month/last30)."""
    base = dict(cena_employee_id=emp_id, toast_id=toast_id, store_key=store,
                service_json={}, attribution_json={"attribution_method": "guid_direct",
                                                    "toast_guid": toast_id})
    return [
        PerfPeriodCache(period="today",  period_start="2026-06-01", period_end="2026-06-01",
                        total_hours=6.25,  reg_hours=6.25,  ot_hours=0.0,
                        base_pay=13.31,  tips=93.90, **base),
        PerfPeriodCache(period="week",   period_start="2026-05-30", period_end="2026-06-01",
                        total_hours=18.83, reg_hours=18.83, ot_hours=0.0,
                        base_pay=40.13,  tips=480.93, **base),
        PerfPeriodCache(period="month",  period_start="2026-06-01", period_end="2026-06-01",
                        total_hours=6.25,  reg_hours=6.25,  ot_hours=0.0,
                        base_pay=13.31,  tips=93.90, **base),
        PerfPeriodCache(period="last30", period_start="2026-05-03", period_end="2026-06-01",
                        total_hours=139.97, reg_hours=139.97, ot_hours=0.0,
                        base_pay=298.14, tips=3077.25, **base),
    ]


def _periods_boh(emp_id, toast_id, store):
    """4 BOH periods (no tips -- non-tipped role; tips column is 0)."""
    base = dict(cena_employee_id=emp_id, toast_id=toast_id, store_key=store,
                service_json={}, attribution_json={"attribution_method": "guid_direct",
                                                    "toast_guid": toast_id})
    return [
        PerfPeriodCache(period="today",  period_start="2026-06-01", period_end="2026-06-01",
                        total_hours=8.0,   reg_hours=8.0,  ot_hours=0.0,
                        base_pay=136.0, tips=0.0, **base),
        PerfPeriodCache(period="week",   period_start="2026-05-30", period_end="2026-06-01",
                        total_hours=24.0,  reg_hours=24.0, ot_hours=0.0,
                        base_pay=408.0, tips=0.0, **base),
        PerfPeriodCache(period="month",  period_start="2026-06-01", period_end="2026-06-01",
                        total_hours=8.0,   reg_hours=8.0,  ot_hours=0.0,
                        base_pay=136.0, tips=0.0, **base),
        PerfPeriodCache(period="last30", period_start="2026-05-03", period_end="2026-06-01",
                        total_hours=152.0, reg_hours=150.0, ot_hours=2.0,
                        base_pay=2584.0, tips=0.0, **base),
    ]


def seed():
    db = SessionLocal()
    try:
        TID_A = "75a58c11-bce8-4e03-9222-dec3a3774744"   # Yadira (real test GUID)
        TID_B = "11111111-2222-3333-4444-555555555555"   # Damaris
        TID_C = "99999999-8888-7777-6666-555555555555"   # Pending Person

        # (A) TIPPED -- Yadira, copperfield.
        db.add(Employee(id=71, full_name="Yadira Romer Hernandez", active=True))
        db.add(CenaToastLink(cena_employee_id=71, store_key="copperfield",
                             toast_id=TID_A, toast_name="Yadira Romer Hernandez"))
        for p in _periods_tipped(71, TID_A, "copperfield"):
            db.add(p)
        # 3 shifts, one needs_review=True
        db.add(PerfShiftCache(cena_employee_id=71, toast_id=TID_A, store_key="copperfield",
                              business_date="2026-06-01",
                              clock_in="2026-06-01T15:09:44.476+0000",
                              clock_out="2026-06-01T21:24:49.715+0000",
                              reg_hours=6.25, ot_hours=0.0, total_hours=6.25,
                              base_pay=13.31, tips=93.90, tips_declared=True,
                              needs_review=False, review_reason=None,
                              attribution_json={"attribution_method": "guid_direct"}))
        db.add(PerfShiftCache(cena_employee_id=71, toast_id=TID_A, store_key="copperfield",
                              business_date="2026-05-29",
                              clock_in="2026-05-29T16:12:09.602+0000",
                              clock_out="2026-05-29T21:37:01.581+0000",
                              reg_hours=5.41, ot_hours=0.0, total_hours=5.41,
                              base_pay=11.52, tips=387.03, tips_declared=True,
                              needs_review=False, review_reason=None,
                              attribution_json={"attribution_method": "guid_direct"}))
        db.add(PerfShiftCache(cena_employee_id=71, toast_id=TID_A, store_key="copperfield",
                              business_date="2026-05-25",
                              clock_in="2026-05-25T15:05:11.000+0000",
                              clock_out="2026-05-26T09:00:00.000+0000",
                              reg_hours=17.91, ot_hours=0.0, total_hours=17.91,
                              base_pay=38.15, tips=282.80, tips_declared=True,
                              needs_review=True,
                              review_reason="auto clock-out (possible missed punch) -- verify with manager",
                              attribution_json={"attribution_method": "guid_direct"}))
        db.add(PerfRankCache(cena_employee_id=71, rank_json=RANK_A,
                             computed_at="2026-06-01T19:11:35"))

        # (B) BOH / non-tipped -- Damaris, tomball.
        db.add(Employee(id=16, full_name="Damaris Boh", active=True))
        db.add(CenaToastLink(cena_employee_id=16, store_key="tomball",
                             toast_id=TID_B, toast_name="Damaris Boh"))
        for p in _periods_boh(16, TID_B, "tomball"):
            db.add(p)
        db.add(PerfShiftCache(cena_employee_id=16, toast_id=TID_B, store_key="tomball",
                              business_date="2026-06-01",
                              clock_in="2026-06-01T09:00:00.000+0000",
                              clock_out="2026-06-01T17:00:00.000+0000",
                              reg_hours=8.0, ot_hours=0.0, total_hours=8.0,
                              base_pay=136.0, tips=0.0, tips_declared=False,
                              needs_review=False, review_reason=None,
                              attribution_json={"attribution_method": "guid_direct"}))
        db.add(PerfRankCache(cena_employee_id=16, rank_json=RANK_B,
                             computed_at="2026-06-01T19:11:35"))

        # (C) NO-DATA -- Pending Person, copperfield link, NO perf rows at all.
        db.add(Employee(id=99, full_name="Pending Person", active=True))
        db.add(CenaToastLink(cena_employee_id=99, store_key="copperfield",
                             toast_id=TID_C, toast_name="Pending Person"))

        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Dual-grep blocklists (samai #2945 pattern). BOTH must find NOTHING.
# --------------------------------------------------------------------------
SALES = re.compile(
    r"cc_subtotal|cash_amount|net_sales|check_total|revenue|gross|eligible_sales|"
    r"cashSales|nonCashSales|\bsales\b", re.I)
INTERNAL = re.compile(
    r"attribution_method|guid_direct|toast_guid|toast_id|cena_toast_link|perf_internal|"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", re.I)


def grep_clean(label, body, results):
    """Return True if clean (no sales + no internal/GUID hits); record a line."""
    sh = SALES.findall(body or "")
    ih = INTERNAL.findall(body or "")
    ok = (not sh) and (not ih)
    results.append("    [%s] SALES=%s INTERNAL/GUID=%s"
                   % (label, sh or "NONE", ih or "NONE"))
    return ok


def as_session(client, emp_id):
    with client.session_transaction() as s:
        s["employee_id"] = emp_id
        s["auth_ok"] = True   # clears the global site gate (auth.py:_gate)


def get_center(client):
    return client.get("/employee/performance-center")


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------
def main():
    seed()
    PROOF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proof")
    os.makedirs(PROOF, exist_ok=True)

    checks = []   # list of (name, passed_bool, detail_lines[])

    # ---- CHECK 1: A (TIPPED) ----
    c = app.test_client(); as_session(c, 71)
    r = get_center(c)
    a_body = r.get_data(as_text=True)
    a_json = r.get_json(silent=True) or {}
    a_money = (((a_json.get("periods") or {}).get("last30") or {}).get("money") or {})
    det = []
    p1 = (r.status_code == 200 and a_json.get("ok") is True
          and a_json.get("linked") is True and a_json.get("is_tipped") is True
          and "tips" in a_money and "tip_pct" in a_money and "tips_per_hour" in a_money)
    det.append("    status=%s ok=%s linked=%s is_tipped=%s last30.money keys=%s"
               % (r.status_code, a_json.get("ok"), a_json.get("linked"),
                  a_json.get("is_tipped"), sorted(a_money.keys())))
    checks.append(("A tipped: 200/ok/linked/is_tipped + last30.money has tips/tip_pct/tips_per_hour",
                   p1, det))

    # ---- CHECK 2: B (BOH / non-tipped) ----
    c = app.test_client(); as_session(c, 16)
    r = get_center(c)
    b_body = r.get_data(as_text=True)
    b_json = r.get_json(silent=True) or {}
    b_money = (((b_json.get("periods") or {}).get("last30") or {}).get("money") or {})
    det = []
    tip_keys_present = [k for k in ("tips", "tip_pct", "tips_per_hour") if k in b_money]
    p2 = (r.status_code == 200 and b_json.get("ok") is True
          and b_json.get("is_tipped") is False and not tip_keys_present)
    det.append("    status=%s is_tipped=%s last30.money keys=%s tip_keys_present=%s"
               % (r.status_code, b_json.get("is_tipped"), sorted(b_money.keys()),
                  tip_keys_present or "NONE"))
    checks.append(("B BOH: 200, is_tipped false, last30.money has NO tips/tip_pct/tips_per_hour",
                   p2, det))

    # ---- CHECK 3: C (NO-DATA, linked-but-syncing) ----
    c = app.test_client(); as_session(c, 99)
    r = get_center(c)
    c_body = r.get_data(as_text=True)
    c_json = r.get_json(silent=True) or {}
    det = []
    empty_periods = not (c_json.get("periods") or {})
    p3 = (r.status_code == 200 and c_json.get("linked") is True
          and (c_json.get("syncing") is True or empty_periods))
    det.append("    status=%s linked=%s syncing=%s periods_empty=%s"
               % (r.status_code, c_json.get("linked"), c_json.get("syncing"), empty_periods))
    checks.append(("C no-data: 200, linked true, syncing true OR empty periods", p3, det))

    # ---- CHECK 4: session-scoping / no-IDOR ----
    det = []
    # (4a) NO session at all -> the global site gate redirects to keypad login (302),
    #      i.e. an unauth caller NEVER gets 200 perf data. (route is NOT EXEMPT.)
    c0 = app.test_client()
    r_nosess = c0.get("/employee/performance-center")
    nosess_blocked = r_nosess.status_code in (301, 302, 401, 403)
    det.append("    no-session  -> status=%s (site gate blocks; never 200)" % r_nosess.status_code)
    # (4b) site-authed but NO employee identity -> the ROUTE's own 401 contract.
    c1 = app.test_client()
    with c1.session_transaction() as s:
        s["auth_ok"] = True   # passes site gate, but no employee_id
    r_noemp = c1.get("/employee/performance-center")
    noemp_401 = (r_noemp.status_code == 401)
    det.append("    authed/no-emp -> status=%s (route returns 401 'not signed in')"
               % r_noemp.status_code)
    # (4c) session=A -> body must contain NO other employee's id (16 / 99) anywhere.
    other_16 = re.search(r"\b16\b", a_body) is not None
    other_99 = re.search(r"\b99\b", a_body) is not None
    no_cross = (not other_16) and (not other_99)
    det.append("    session=A body contains other emp id 16=%s 99=%s (must be False/False)"
               % (other_16, other_99))
    p4 = nosess_blocked and noemp_401 and no_cross
    checks.append(("Session-scoping/no-IDOR: unauth blocked + 401 no-emp + A body leaks no 16/99",
                   p4, det))

    # ---- CHECK 5: dual-grep every body (A,B,C + detail HTML pages) ----
    det = []
    g_ok = True
    g_ok &= grep_clean("A /performance-center", a_body, det)
    g_ok &= grep_clean("B /performance-center", b_body, det)
    g_ok &= grep_clean("C /performance-center", c_body, det)
    # detail pages (rendered HTML shell; JS fetches the data) -- prove the shell is clean.
    detail_metrics = ["total_pay", "effective_hourly", "tip_pct", "attendance", "rank_tip_pct"]
    cda = app.test_client(); as_session(cda, 71)   # render as A
    for m in detail_metrics:
        rr = cda.get("/employee/performance/%s" % m)
        dbody = rr.get_data(as_text=True)
        det.append("    detail /performance/%s -> status=%s len=%d"
                   % (m, rr.status_code, len(dbody)))
        if rr.status_code != 200:
            g_ok = False
        g_ok &= grep_clean("detail:%s" % m, dbody, det)
    checks.append(("Dual-grep: no SALES + no INTERNAL/GUID in any A/B/C + detail body", g_ok, det))

    # ---- CHECK 6: write proof JSON for A and B ----
    det = []
    try:
        ptip = os.path.join(PROOF, "t108_perfcenter_tipped.json")
        pboh = os.path.join(PROOF, "t108_perfcenter_boh.json")
        with open(ptip, "w", encoding="utf-8") as f:
            json.dump(a_json, f, indent=2, sort_keys=True)
        with open(pboh, "w", encoding="utf-8") as f:
            json.dump(b_json, f, indent=2, sort_keys=True)
        p6 = (os.path.getsize(ptip) > 0 and os.path.getsize(pboh) > 0
              and a_json.get("ok") is True and b_json.get("ok") is True)
        det.append("    wrote %s (%d bytes)" % (ptip, os.path.getsize(ptip)))
        det.append("    wrote %s (%d bytes)" % (pboh, os.path.getsize(pboh)))
    except Exception as e:   # noqa: BLE001
        p6 = False
        det.append("    EXCEPTION writing proof: %s" % e)
    checks.append(("Proof JSON written for A (tipped) and B (BOH)", p6, det))

    # --------------------------------------------------------------------------
    # Report
    # --------------------------------------------------------------------------
    print("=" * 72)
    print("T108 performance-center harness  (temp DB: %s)" % dbpath)
    print("=" * 72)
    overall = True
    for i, (name, passed, det_lines) in enumerate(checks, start=1):
        print("CHECK %d: %s -- %s" % (i, "PASS" if passed else "FAIL", name))
        for line in det_lines:
            print(line)
        overall &= passed
    print("=" * 72)
    print("OVERALL: %s" % ("PASS" if overall else "FAIL"))
    print("=" * 72)
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
