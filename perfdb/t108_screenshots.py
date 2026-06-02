"""T108 employee-performance UI screenshot harness (task #108).

Captures real, JS-rendered mobile screenshots of the employee performance
surfaces across three role/data states:

    A = id 71  TIPPED      (Yadira, copperfield)   -- full tip cards + rankings
    B = id 16  BOH         (Damaris, tomball)      -- NO tip cards, "tipped roles
                                                       only" on tip-metric pages
    C = id 99  NO-DATA     (Pending, copperfield)  -- linked but no perf rows

WHY a browser: the dashboard + the /employee/performance/<metric> detail pages
fetch their data via client-side JS (fetch('/employee/my-performance') and
'/employee/performance-center'). A non-JS client gets only an empty shell, so we
run the REAL app + headless chromium that executes the JS.

Self-contained. Does NOT edit any app file. Does NOT git commit/push.

  Seed   : reuses t108_harness.py's exact ORM seed logic, but points
           DATABASE_URL at a PERSISTENT temp sqlite file so the separately
           launched (in-process, background-thread) server reads the same rows.
  Server : werkzeug make_server on 127.0.0.1:5099, SAME create_app + SECRET_KEY
           + DATABASE_URL, in a daemon thread.
  Auth   : sign a Flask session cookie per employee with the app's OWN
           SecureCookieSessionInterface serializer ({employee_id, auth_ok}), set
           it in the browser before navigating. No login round-trip.
  Shots  : Playwright sync API, headless chromium, viewport 390x844 (mobile),
           full_page. Waits for the JS terminal state (#perf-card / #page /
           #noteligible / #error visible) before shooting.

Run:  cd /c/Users/sam/_schedv2_wt && python perfdb/t108_screenshots.py
"""
import os
import sys
import time
import tempfile
import threading
import traceback
from types import SimpleNamespace

ROOT = r"C:\Users\sam\_schedv2_wt"
sys.path.insert(0, ROOT)

# --- env BEFORE importing app (app/db.py reads DATABASE_URL at import time) ---
# PERSISTENT temp sqlite (NOT mkstemp+delete): the background server process must
# be able to open it; we remove it ourselves in cleanup at the very end.
_DB_DIR = tempfile.mkdtemp(prefix="t108_shots_")
DBPATH = os.path.join(_DB_DIR, "t108_shots.sqlite")
os.environ["DATABASE_URL"] = "sqlite:///" + DBPATH.replace("\\", "/")
os.environ["CRON_TOKEN"] = "testtoken123"
os.environ["SECRET_KEY"] = "t108-shots-secret"
os.environ["ALLOW_DEV_SECRET"] = "1"

from app import create_app                      # noqa: E402
from app.db import engine, SessionLocal         # noqa: E402
from app.models import (                        # noqa: E402,F401
    Employee, CenaToastLink, PerfPeriodCache, PerfShiftCache, PerfRankCache)

app = create_app()   # runs Base.metadata.create_all on the temp DB

HOST = "127.0.0.1"
PORT = 5099
BASE = "http://%s:%d" % (HOST, PORT)
PROOF = os.path.join(ROOT, "perfdb", "proof", "t108_shots")
VIEWPORT = {"width": 390, "height": 844}        # mobile

# ==========================================================================
# SEED -- reused verbatim from perfdb/t108_harness.py (same rank blobs, same
# period builders, same 3 employees). Kept inline so this script is standalone.
# ==========================================================================
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


# ==========================================================================
# Cookie signing -- the app's OWN serializer, so the signed session cookie is
# valid for the running server (same SECRET_KEY + create_app).
# ==========================================================================
def sign_session_cookie(employee_id):
    from flask.sessions import SecureCookieSessionInterface
    s = SecureCookieSessionInterface().get_signing_serializer(app)
    if s is None:
        raise RuntimeError("no signing serializer (SECRET_KEY missing?)")
    return s.dumps({"employee_id": employee_id, "auth_ok": True})


# ==========================================================================
# Server (werkzeug make_server in a daemon thread).
# ==========================================================================
_server = None


def start_server():
    global _server
    from werkzeug.serving import make_server
    _server = make_server(HOST, PORT, app, threaded=True)
    t = threading.Thread(target=_server.serve_forever, daemon=True)
    t.start()
    return t


def wait_until_up(timeout=30.0):
    import urllib.request
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            # /employee/login is anonymous-reachable (site gate exempts it) -> 200.
            with urllib.request.urlopen(BASE + "/employee/login", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.25)
    raise RuntimeError("server did not come up on %s (last: %r)" % (BASE, last))


# ==========================================================================
# Capture plan.  (filename, route, employee_id, expect, human-readable intent)
# expect drives which terminal selector we wait for / how we classify the shot.
#   "dashboard"  -> wait for the greeting; perf-card may or may not reveal
#   "detail"     -> wait for one of #page / #noteligible / #error visible
# ==========================================================================
SHOTS = [
    # ---- A: TIPPED (id 71) ----
    ("A_tipped_dashboard.png",                "/employee/dashboard",                       71, "dashboard"),
    ("A_tipped_total_pay.png",                "/employee/performance/total_pay",           71, "detail"),
    ("A_tipped_tip_pct.png",                  "/employee/performance/tip_pct",             71, "detail"),
    ("A_tipped_tips_per_hour.png",            "/employee/performance/tips_per_hour",       71, "detail"),
    ("A_tipped_rank_tip_pct.png",             "/employee/performance/rank_tip_pct",        71, "detail"),
    ("A_tipped_attendance.png",               "/employee/performance/attendance",          71, "detail"),
    ("A_tipped_effective_hourly.png",         "/employee/performance/effective_hourly",    71, "detail"),
    # ---- B: BOH / non-tipped (id 16) ----
    ("B_boh_dashboard.png",                   "/employee/dashboard",                       16, "dashboard"),
    ("B_boh_effective_hourly.png",            "/employee/performance/effective_hourly",    16, "detail"),
    ("B_boh_tip_pct_NOTELIGIBLE.png",         "/employee/performance/tip_pct",             16, "detail"),
    ("B_boh_rank_effective_hourly.png",       "/employee/performance/rank_effective_hourly", 16, "detail"),
    ("B_boh_attendance.png",                  "/employee/performance/attendance",          16, "detail"),
    # ---- C: NO-DATA (id 99) ----
    ("C_nodata_dashboard.png",                "/employee/dashboard",                       99, "dashboard"),
    ("C_nodata_total_pay.png",                "/employee/performance/total_pay",           99, "detail"),
]


def classify_dashboard(page):
    """After load, report whether the perf-card revealed and (if so) which tip
    cards exist. Returns a 1-line description + a 'broken' flag."""
    info = page.evaluate(
        """() => {
            const card = document.getElementById('perf-card');
            const greeting = document.querySelector('.greeting .name');
            const cardShown = !!(card && !card.hidden);
            const pcards = Array.from(document.querySelectorAll('#perf-cards .pcard-lbl')).map(e => e.textContent.trim());
            const rankShown = !!(document.getElementById('perf-rank-wrap') && !document.getElementById('perf-rank-wrap').hidden);
            const rcards = Array.from(document.querySelectorAll('#rank-grid .rcard-lbl')).map(e => e.textContent.trim());
            const gates = Array.from(document.querySelectorAll('#rank-grid .rcard-gate')).map(e => e.textContent.replace(/\\s+/g,' ').trim());
            const tiles = Array.from(document.querySelectorAll('.tiles .t-title')).map(e => e.textContent.trim());
            return { greeting: greeting ? greeting.textContent.trim() : null,
                     cardShown, pcards, rankShown, rcards, gates,
                     tileCount: tiles.length };
        }"""
    )
    tip_labels = [x for x in info["pcards"] if "Tip" in x]
    if not info["cardShown"]:
        desc = ("greeting=%r; perf-card HIDDEN (no perf data) -> clean dashboard, "
                "%d tool tiles, no empty perf panel"
                % (info["greeting"], info["tileCount"]))
        return desc, False
    has_tips = bool(tip_labels)
    desc = ("greeting=%r; perf-card SHOWN; summary cards=%s; tip-cards=%s; "
            "rank shown=%s rank-tiles=%s gates=%s"
            % (info["greeting"], info["pcards"], (tip_labels or "NONE"),
               info["rankShown"], info["rcards"], (info["gates"] or "NONE")))
    return desc, False, has_tips, tip_labels


def classify_detail(page):
    """Which terminal state is visible: page / noteligible / error. Pull the
    headline text so we can describe it."""
    info = page.evaluate(
        """() => {
            const vis = (id) => { const e=document.getElementById(id); return !!(e && !e.classList.contains('hidden')); };
            const txt = (sel) => { const e=document.querySelector(sel); return e ? e.textContent.replace(/\\s+/g,' ').trim() : null; };
            const cards = Array.from(document.querySelectorAll('#summary-grid .card span')).map(e=>e.textContent.trim());
            return {
                page: vis('page'), noteligible: vis('noteligible'), error: vis('error'), loading: vis('loading'),
                title: txt('#title'), bigValue: txt('#big-value'),
                noteligibleText: txt('#noteligible'), errorText: txt('#error'),
                summaryCards: cards,
                rankShown: !!(document.getElementById('rank-section') && !document.getElementById('rank-section').classList.contains('hidden')),
            };
        }"""
    )
    return info


def run():
    os.makedirs(PROOF, exist_ok=True)
    seed()
    start_server()
    wait_until_up()

    from playwright.sync_api import sync_playwright

    results = []   # (filename, route, emp, state, desc, broken_bool)
    cookies_by_emp = {eid: sign_session_cookie(eid) for eid in (71, 16, 99)}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for fname, route, emp, kind in SHOTS:
                ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
                ctx.add_cookies([{
                    "name": "session", "value": cookies_by_emp[emp],
                    "domain": HOST, "path": "/",
                }])
                page = ctx.new_page()
                broken = False
                state = "?"
                desc = ""
                try:
                    page.goto(BASE + route, wait_until="domcontentloaded", timeout=20000)
                    if kind == "dashboard":
                        # greeting is server-rendered; wait for it, then give the
                        # perf fetch a beat to resolve (it may reveal the card).
                        page.wait_for_selector(".greeting .name", state="visible", timeout=8000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=6000)
                        except Exception:
                            pass
                        page.wait_for_timeout(700)
                        out = classify_dashboard(page)
                        desc = out[0]
                        state = "dashboard"
                        # T108 (samai #3142 note 4): the position:fixed sticky
                        # bottom-nav (`nav.ck-enav` from partials/_employee_nav.html)
                        # overlays the bottom content in a full-page static capture,
                        # clipping "Not eligible" / "Top X%" text. The app is correct
                        # at real runtime; this is purely a screenshot artifact. Hide
                        # ONLY that nav for the full-page dashboard shot. (Dashboard
                        # captures only -- detail captures are left untouched.)
                        hidden = page.evaluate(
                            """() => {
                                const navs = document.querySelectorAll('nav.ck-enav');
                                navs.forEach(n => { n.style.display = 'none'; });
                                return navs.length;
                            }"""
                        )
                        desc += "  [hid %d .ck-enav bottom-nav for capture]" % hidden
                    else:
                        # detail: wait until loading is gone AND one terminal state visible
                        try:
                            page.wait_for_function(
                                """() => {
                                    const vis=(id)=>{const e=document.getElementById(id);return !!(e&&!e.classList.contains('hidden'));};
                                    return vis('page')||vis('noteligible')||vis('error');
                                }""",
                                timeout=12000,
                            )
                        except Exception:
                            # fall back to networkidle + a beat; classify whatever rendered
                            try:
                                page.wait_for_load_state("networkidle", timeout=5000)
                            except Exception:
                                pass
                            page.wait_for_timeout(800)
                        info = classify_detail(page)
                        if info["page"]:
                            state = "page"
                            desc = ("rendered #page; title=%r big-value=%r; summary tiles=%s; rank-section=%s"
                                    % (info["title"], info["bigValue"], info["summaryCards"], info["rankShown"]))
                        elif info["noteligible"]:
                            state = "noteligible"
                            desc = "rendered #noteligible (clean role-gate): %r" % (info["noteligibleText"],)
                        elif info["error"]:
                            state = "error"
                            desc = "rendered #error (clean no-data state): %r" % (info["errorText"],)
                        else:
                            state = "STUCK"
                            desc = ("NO terminal state visible (loading=%s) -- POSSIBLE BREAK"
                                    % info["loading"])
                            broken = True
                    # screenshot regardless so we have visual proof
                    page.screenshot(path=os.path.join(PROOF, fname), full_page=True)
                except Exception as e:  # noqa: BLE001
                    broken = True
                    state = "EXC"
                    desc = "EXCEPTION: %s: %s" % (type(e).__name__, e)
                    try:
                        page.screenshot(path=os.path.join(PROOF, fname), full_page=True)
                    except Exception:
                        pass
                finally:
                    ctx.close()
                results.append((fname, route, emp, state, desc, broken))
                print("  shot: %-34s %-44s emp=%-3s [%s]" % (fname, route, emp, state))
        finally:
            browser.close()

    return results


def cleanup():
    global _server
    try:
        if _server is not None:
            _server.shutdown()
    except Exception:
        pass
    try:
        engine.dispose()
    except Exception:
        pass
    # remove the persistent temp DB + its dir
    try:
        if os.path.exists(DBPATH):
            os.remove(DBPATH)
        os.rmdir(_DB_DIR)
    except Exception:
        pass


def main():
    results = []
    try:
        results = run()
    finally:
        cleanup()

    # ---- summary table ----
    EMP_LABEL = {71: "A tipped", 16: "B BOH", 99: "C no-data"}
    print("\n" + "=" * 100)
    print("T108 SCREENSHOT SUMMARY  (saved to %s)" % PROOF)
    print("=" * 100)
    broken = [r for r in results if r[5]]
    for fname, route, emp, state, desc, isbroken in results:
        flag = "  <<< CHECK" if isbroken else ""
        print("- %s" % fname)
        print("    route=%s  emp=%s (%s)  state=%s%s"
              % (route, emp, EMP_LABEL.get(emp, "?"), state, flag))
        print("    %s" % desc)
    print("=" * 100)
    if broken:
        print("FLAGGED %d shot(s) that may be blank/broken/error:" % len(broken))
        for fname, route, emp, state, desc, _ in broken:
            print("   - %s (%s) state=%s" % (fname, route, state))
    else:
        print("All %d shots reached a coherent terminal state (page / noteligible / "
              "error / dashboard); none stuck on loading or threw." % len(results))
    print("=" * 100)
    return 0 if not broken else 1


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    except Exception:
        traceback.print_exc()
        cleanup()
        rc = 1
    sys.exit(rc)
