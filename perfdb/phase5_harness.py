"""Phase 5.1 harness (Sam #3005..#3019): full LOCAL round-trip for the pilot ranking.
  create_app + temp sqlite  ->  seed 6 pilot employees + links
  ->  pilot_push() to the REAL /cron/perf-push receiver (exercises the sales-wall guard)
  ->  GET /employee/my-performance per employee via SIGNED COOKIE (no PIN)  [= samai option-ii payloads]
  ->  Playwright screenshots of the ranking surface (Yadira rich + Damaris BOH-gated)
No prod push. ASCII-safe prints. Outputs _phase5_payload_<id>.json + _phase5_*.png + _phase5_rendered.html
"""
import os, sys, json, re, tempfile, threading, time, sqlite3, urllib.request

WT = r"C:\Users\sam\_schedv2_wt"; PERFDB = r"C:\Users\sam\cena-perfdb"
for p in (WT, PERFDB):
    if p not in sys.path:
        sys.path.insert(0, p)
fd, dbpath = tempfile.mkstemp(suffix="_phase5.sqlite"); os.close(fd)
os.environ["DATABASE_URL"] = "sqlite:///" + dbpath.replace("\\", "/")
os.environ["SECRET_KEY"] = "test-secret-phase5"; os.environ["ALLOW_DEV_SECRET"] = "1"
os.environ["CRON_TOKEN"] = "localtest-phase5"          # receiver + pilot_push share this
os.environ["PILOT_PUSH_URL"] = "http://127.0.0.1:5098"

from app import create_app
from app.db import engine
from sqlalchemy import text, inspect
import pilot_phase5 as P

app = create_app()
insp = inspect(engine); TBL = insp.get_table_names()
emp_t = "employees" if "employees" in TBL else "employee"
link_t = next((t for t in TBL if "toast_link" in t), None)
print("tables present: perf_rank_cache=%s perf_period_cache=%s perf_shift_cache=%s link=%s"
      % ("perf_rank_cache" in TBL, "perf_period_cache" in TBL, "perf_shift_cache" in TBL, link_t))


def seed(table, vals):
    row = dict(vals)
    for c in insp.get_columns(table):
        n = c["name"]
        if n in row or n == "id":
            continue
        if (not c["nullable"]) and c.get("default") is None:
            t = str(c["type"]).upper()
            row[n] = (0 if any(x in t for x in ("INT", "REAL", "FLOAT", "NUMERIC", "DECIMAL"))
                      else ("1970-01-01 00:00:00" if any(x in t for x in ("DATE", "TIME")) else ""))
    with engine.begin() as conn:
        conn.execute(text("INSERT OR REPLACE INTO %s (%s) VALUES (%s)"
                          % (table, ",".join(row), ",".join(":" + k for k in row))), row)


# seed the 6 pilot employees + their confirmed links (Drew = 2 stores)
for e in P.ALLOWLIST:
    seed(emp_t, {"id": e["cena_employee_id"], "full_name": e["full_name"], "active": 1})
if link_t:
    for e in P.ALLOWLIST:
        seed(link_t, {"cena_employee_id": e["cena_employee_id"], "store_key": e["store_key"],
                      "toast_id": e["toast_id"], "toast_name": e["full_name"]})
for sa_t in [t for t in TBL if "store_assignment" in t or t == "employee_store_assignments"]:
    for e in P.ALLOWLIST:
        try:
            seed(sa_t, {"employee_id": e["cena_employee_id"], "store_key": e["store_key"]})
        except Exception:
            pass

from werkzeug.serving import make_server
srv = make_server("127.0.0.1", 5098, app)
threading.Thread(target=srv.serve_forever, daemon=True).start(); time.sleep(1.0)

print("=" * 70); print("STEP 1 -- pilot_push() to the REAL receiver (sales-wall guard live)"); print("=" * 70)
push_res = P.pilot_push(base_url="http://127.0.0.1:5098")

print("=" * 70); print("STEP 2 -- per-employee /employee/my-performance payloads (samai option-ii)"); print("=" * 70)
from flask.sessions import SecureCookieSessionInterface
ser = SecureCookieSessionInterface().get_signing_serializer(app)
SALES = re.compile(r"cashsales|noncashsales|eligible_sales|sales_attributed|sales_dollars|"
                   r"\bsales\b|\bgross\b|\brevenue\b|\bdrawer\b|gratuityservice|sales_basis", re.I)
GUIDRE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
INTERNAL = re.compile(r"attribution|toast_guid|guid_direct|perf_internal|hourly_rate|wage|server_guid", re.I)
all_clean = True
for cid in sorted(P.ALLOWED_IDS):
    cookie = ser.dumps({"employee_id": cid, "auth_ok": True})
    req = urllib.request.Request("http://127.0.0.1:5098/employee/my-performance",
                                 headers={"Cookie": "session=" + cookie, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        payload = r.read().decode("utf-8")
    open(os.path.join(r"C:\Users\sam", "_phase5_payload_%s.json" % cid), "w", encoding="utf-8").write(payload)
    d = json.loads(payload)
    s_hit = SALES.findall(payload); g_hit = GUIDRE.findall(payload); i_hit = INTERNAL.findall(payload)
    has_rank = "ranking" in d
    has_tp = "tip_percent" in payload
    all_clean = all_clean and not s_hit and not g_hit and not i_hit
    print("  id=%-3s linked=%s ranking=%s len=%-5d SALES=%s GUID=%s INTERNAL=%s tip%%=%s"
          % (cid, d.get("linked"), has_rank, len(payload),
             s_hit or "0", len(g_hit), i_hit or "0", has_tp))
print("-" * 70)
print("PAYLOAD GREP (all 6):", "PASS -- 0 sales / 0 GUID / 0 internal in any payload" if all_clean else "FAIL")

print("=" * 70); print("STEP 3 -- screenshots of the ranking surface"); print("=" * 70)
from playwright.sync_api import sync_playwright
OUT = r"C:\Users\sam"; URL = "http://127.0.0.1:5098/employee/dashboard"
shots = []
with sync_playwright() as pw:
    br = pw.chromium.launch()
    for cid, tag in [(71, "yadira"), (45, "damaris")]:
        cookie = ser.dumps({"employee_id": cid, "auth_ok": True})
        ctx = br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        ctx.add_cookies([{"name": "session", "value": cookie, "domain": "127.0.0.1", "path": "/"}])
        pg = ctx.new_page(); pg.goto(URL, wait_until="networkidle"); pg.wait_for_timeout(900)
        pg.screenshot(path=os.path.join(OUT, "_phase5_%s_top.png" % tag))
        pg.evaluate("var e=document.getElementById('perf-rank-wrap'); if(e){e.scrollIntoView();}")
        pg.wait_for_timeout(450)
        pg.screenshot(path=os.path.join(OUT, "_phase5_%s_rank.png" % tag))
        pg.screenshot(path=os.path.join(OUT, "_phase5_%s_full.png" % tag), full_page=True)
        if cid == 71:
            open(os.path.join(OUT, "_phase5_rendered.html"), "w", encoding="utf-8").write(pg.content())
        shots.append(tag); ctx.close()
    # desktop overview (Yadira)
    cookie = ser.dumps({"employee_id": 71, "auth_ok": True})
    ctx2 = br.new_context(viewport={"width": 1280, "height": 900})
    ctx2.add_cookies([{"name": "session", "value": cookie, "domain": "127.0.0.1", "path": "/"}])
    pg2 = ctx2.new_page(); pg2.goto(URL, wait_until="networkidle"); pg2.wait_for_timeout(900)
    pg2.screenshot(path=os.path.join(OUT, "_phase5_desktop.png"), full_page=True)
    ctx2.close(); br.close()
print("screenshots:", shots, "+ desktop")

# DOM sales-grep on the rendered ranking surface (Sam #3019 -- rendered DOM is a named surface)
html = open(os.path.join(OUT, "_phase5_rendered.html"), encoding="utf-8").read()
dom_sales = SALES.findall(html)
print("RENDERED-DOM grep:", "PASS -- 0 sales in DOM" if not dom_sales else ("FAIL " + str(set(dom_sales))))

srv.shutdown()
try:
    os.remove(dbpath)
except Exception:
    pass
print("=" * 70)
print("HARNESS DONE -- payloads _phase5_payload_*.json, shots _phase5_*.png, _phase5_rendered.html")
print("=" * 70)
