"""Schedules V2 -- "Link" tab Toast backend (ckbro, 2026-05-31).

Two manager-only read endpoints that power the Team > Link tab, which pairs
each Cena scheduling employee with their Toast POS identity and then surfaces
that employee's Toast labor + server-performance for the store.

REUSE, not reinvent (per the build brief):
  - Toast API access: app.services.toast_client (ToastClient.shared(),
    restaurant_guids(), fetch_employees, fetch_time_entries). Creds are env
    vars handled INSIDE toast_client -- we never touch them here.
  - The Cena<->Toast name+phone MATCH logic: app.services.sling_reports already
    cross-references Cena people against Toast /labor/v1/employees by a
    normalized "first last" key with a last-name-only fallback, formatting the
    phone via _fmt_phone. We import _fmt_phone and mirror that exact normalize
    rule here (the sling matcher is a name->phone MAP builder, not a 1:1 pair
    emitter, so we run the same rule to emit {cena<->toast} pairs). We do NOT
    use app/services/toast_match.py.
  - Per-employee PERFORMANCE (server sales): app.services.toast_reports
    .server_perf_report -- the same pull the Operations > Performance page uses
    (reports.py:411 `toast_reports.server_perf_report(start, end, location,
    role_filter=...)`). It returns rows keyed by employee NAME within
    by_location[loc]["rows"]; we pull for the resolved store and pick the row
    whose name matches the Toast employee.
  - The Cena store roster: app.services.team_roster.team_roster(db,
    location=<store>) -> stores[].employees[] each {id, full_name, phone, ...}.

Conventions mirror app.web.schedules_v2_roster: rides store_bp (inheriting
_pull_store 404 + _per_store_gate cross-store), gated @require_level(_MGR) ONLY
(NO @requires_permission -- a prior lockout came from gating a manager action on
a reserved catalog permission), SessionLocal() in try/finally, and never touches
session['partner_auth_ok']. Every Toast call is wrapped so a Toast/creds failure
returns {"ok": false, "error": ...} -- never an uncaught 500.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from flask import jsonify

from app.db import SessionLocal
from app.services.sling_reports import _fmt_phone
from app.services.toast_client import ToastClient, restaurant_guids
from app.web.permissions import require_level
from app.web.schedules_v2 import _MGR, _store
from app.web.store_routes import store_bp

log = logging.getLogger(__name__)

# Recent window for the per-employee timecard/hours pull. Toast's labor API
# rejects any interval "longer than 30 days", and toast_client.fetch_time_entries
# pads the end +1 day (inclusive end), so the REQUEST span = WINDOW + 1. Cap the
# window at 29 -> a 30-day request == Toast's documented max (Sam #2840: 30 here
# over-shot to a 31-day request -> HTTP 400, which starved BOTH the manager Link
# tab and the employee 'My Hours & Pay' panel once real creds went live).
_TIMECARD_WINDOW_DAYS = 29


def _norm_name(first: str, last: str) -> str:
    """Normalized 'first last' match key -- the SAME rule sling_reports uses to
    cross-reference Cena names against Toast employees (lowercased, trimmed)."""
    return f"{(first or '').strip().lower()} {(last or '').strip().lower()}".strip()


def _norm_full(full: str) -> str:
    """Normalize a single full-name string the same way (Cena's full_name is one
    field; Toast carries first/last separately)."""
    return " ".join((full or "").strip().lower().split())


def _toast_name(e: dict) -> str:
    """Display 'First Last' for a Toast employee (chosenName falls back to
    firstName -- same precedence the sling matcher + schedule_report use)."""
    first = (e.get("firstName") or e.get("chosenName") or "").strip()
    last = (e.get("lastName") or "").strip()
    return (f"{first} {last}".strip()
            or e.get("email") or (e.get("guid") or "?")[:8])


def _toast_phone(e: dict) -> str:
    """Formatted phone for a Toast employee (Toast carries phoneNumber +
    phoneNumberCountryCode); '' when absent. Reuses sling_reports._fmt_phone."""
    raw = e.get("phoneNumber")
    if not raw:
        return ""
    return _fmt_phone(raw, e.get("phoneNumberCountryCode") or "")


def _cena_roster(db, store: str) -> list[dict]:
    """THIS store's Cena scheduling roster as [{emp_id, name, phone}].

    Cleanest source = team_roster(db, location=store): it already resolves the
    store-based roster (EmployeeStoreAssignment) + names + phones, returning
    stores[].employees[]. We narrow to the requested store and flatten."""
    from app.services.team_roster import team_roster
    data = team_roster(db, location=store)
    out: list[dict] = []
    for s in data.get("stores", []):
        if s.get("store_key") != store:
            continue
        for e in s.get("employees", []):
            out.append({
                "emp_id": e.get("id"),
                "name": e.get("full_name") or "",
                "phone": e.get("phone") or "",
            })
    return out


def _ignored_link_ids(db, store: str) -> tuple[set[int], set[str]]:
    """Return ignored Cenas employee ids and Toast ids for this store."""
    from app.models import CenaToastIgnore

    ignored_cena: set[int] = set()
    ignored_toast: set[str] = set()
    rows = (db.query(CenaToastIgnore.source, CenaToastIgnore.source_id)
              .filter(CenaToastIgnore.store_key == store)
              .all())
    for source, source_id in rows:
        sid = str(source_id or "").strip()
        if not sid:
            continue
        if source == "cena":
            try:
                ignored_cena.add(int(sid))
            except (TypeError, ValueError):
                continue
        elif source == "toast":
            ignored_toast.add(sid)
    return ignored_cena, ignored_toast


def _confirmed_link_rows(
    db,
    store: str,
    cena_by_id: dict[int, dict],
    toast_by_id: dict[str, dict],
    ignored_cena: set[int],
    ignored_toast: set[str],
) -> list[dict]:
    """Saved links rendered as first-class rows on the Link tab."""
    from app.models import CenaToastLink

    rows: list[dict] = []
    for link in (db.query(CenaToastLink)
                   .filter(CenaToastLink.store_key == store)
                   .order_by(CenaToastLink.cena_employee_id)
                   .all()):
        try:
            cena_emp_id = int(link.cena_employee_id)
        except (TypeError, ValueError):
            continue
        toast_id = str(link.toast_id or "").strip()
        if cena_emp_id in ignored_cena or toast_id in ignored_toast:
            continue
        cena = cena_by_id.get(cena_emp_id)
        if not cena:
            continue
        toast = toast_by_id.get(toast_id)
        rows.append({
            "cena_emp_id": cena_emp_id,
            "cena_name": cena.get("name") or "",
            "cena_phone": cena.get("phone") or "",
            "toast_id": toast_id,
            "toast_name": (link.toast_name or (toast or {}).get("name") or toast_id[:8]),
            "toast_phone": (toast or {}).get("phone") or "",
        })
    return rows


@store_bp.route("/schedules-v2/toast/match-suggestions", methods=["GET"])
@require_level(_MGR)
def sv2_toast_match_suggestions():
    """LINK TAB -- suggest Cena<->Toast employee pairings for THIS store.

    Resolve the location via _store() and its restaurant GUID via
    restaurant_guids(); fetch_employees(location, guid); pull this store's Cena
    roster; match by the reused sling normalize rule (exact 'first last', then a
    last-name-only fallback). ->
      {ok, suggestions:[{cena_emp_id, cena_name, cena_phone, toast_id,
                         toast_name, toast_phone, confidence}],
       unmatched_cena:[{emp_id, name, phone}],
       unmatched_toast:[{toast_id, name, phone}]}
    Any Toast/creds failure -> {ok:false, error} (HTTP 502), never a 500.
    """
    store = _store()
    if not store or store not in ("tomball", "copperfield"):
        # Link is a per-store action; 'both'/partner has no single GUID to match.
        return jsonify({"ok": False,
                        "error": "Select a specific store (Tomball or Copperfield) to link Toast."}), 400

    guids = restaurant_guids()
    guid = guids.get(store)
    if not guid:
        return jsonify({"ok": False,
                        "error": f"No Toast restaurant GUID configured for {store}."}), 502

    try:
        toast_emps = ToastClient.shared().fetch_employees(store, guid) or []
    except Exception as ex:
        log.warning("toast-link: fetch_employees failed for %s: %s", store, ex)
        return jsonify({"ok": False, "error": f"Toast employees unavailable: {ex}"}), 502

    # Build the Toast side: skip deleted. Confirmed/ignored filtering happens
    # after we read the local Link cleanup state.
    toast_records: list[dict] = []
    for e in toast_emps:
        if e.get("deleted"):
            continue
        first = (e.get("firstName") or e.get("chosenName") or "").strip()
        last = (e.get("lastName") or "").strip()
        rec = {
            "toast_id": e.get("guid"),
            "name": _toast_name(e),
            "phone": _toast_phone(e),
            "_full": _norm_name(first, last),
            "_last": last.lower(),
        }
        toast_records.append(rec)

    db = SessionLocal()
    try:
        ignored_cena, ignored_toast = _ignored_link_ids(db, store)
        cena = [c for c in _cena_roster(db, store)
                if c.get("emp_id") not in ignored_cena]
        toast_records = [r for r in toast_records
                         if str(r.get("toast_id") or "") not in ignored_toast]
        toast_by_id = {str(r["toast_id"]): r for r in toast_records if r.get("toast_id")}
        cena_by_id = {int(c["emp_id"]): c for c in cena if c.get("emp_id") is not None}
        confirmed_links = _confirmed_link_rows(
            db, store, cena_by_id, toast_by_id, ignored_cena, ignored_toast)
    finally:
        db.close()

    confirmed_cena = {int(r["cena_emp_id"]) for r in confirmed_links}
    confirmed_toast = {str(r["toast_id"]) for r in confirmed_links}
    matchable_toast = [r for r in toast_records
                       if str(r.get("toast_id") or "") not in confirmed_toast]

    toast_by_full: dict[str, dict] = {}
    toast_by_last: dict[str, list[dict]] = {}
    for rec in matchable_toast:
        if rec["_full"]:
            # First writer wins on a full-name collision (rare); both still
            # appear in unmatched_toast if neither gets claimed.
            toast_by_full.setdefault(rec["_full"], rec)
        if rec["_last"]:
            toast_by_last.setdefault(rec["_last"], []).append(rec)

    suggestions: list[dict] = []
    unmatched_cena: list[dict] = []
    claimed: set[str] = set()  # toast_ids already paired

    for c in cena:
        try:
            cena_emp_id = int(c["emp_id"])
        except (TypeError, ValueError):
            cena_emp_id = None
        if cena_emp_id in confirmed_cena:
            continue
        full = _norm_full(c["name"])
        parts = full.split()
        match = None
        confidence = None
        # 1) exact normalized full-name match
        hit = toast_by_full.get(full)
        if hit and hit["toast_id"] not in claimed:
            match, confidence = hit, "high"
        # 2) last-name-only fallback -- ONLY when exactly one unclaimed Toast
        #    employee shares the last name (avoid mislinking two same-surname).
        if match is None and parts:
            cands = [r for r in toast_by_last.get(parts[-1], [])
                     if r["toast_id"] not in claimed]
            if len(cands) == 1:
                match, confidence = cands[0], "low"
        if match is not None:
            claimed.add(match["toast_id"])
            suggestions.append({
                "cena_emp_id": c["emp_id"],
                "cena_name": c["name"],
                "cena_phone": c["phone"],
                "toast_id": match["toast_id"],
                "toast_name": match["name"],
                "toast_phone": match["phone"],
                "confidence": confidence,
            })
        else:
            unmatched_cena.append({"emp_id": c["emp_id"],
                                   "name": c["name"], "phone": c["phone"]})

    unmatched_toast = [{"toast_id": r["toast_id"], "name": r["name"], "phone": r["phone"]}
                       for r in matchable_toast if r["toast_id"] not in claimed]

    return jsonify({
        "ok": True,
        "confirmed_links": confirmed_links,
        "suggestions": suggestions,
        "unmatched_cena": unmatched_cena,
        "unmatched_toast": unmatched_toast,
    }), 200


@store_bp.route("/schedules-v2/toast/reconcile-profiles", methods=["POST"])
@require_level("partner")
def sv2_toast_reconcile_profiles():
    """Partner-only manual nudge for Toast-only -> Cenas profile creation.

    The scheduled Toast sync runs this automatically; this endpoint gives Sam a
    safe same-day trigger without exposing the cron token.
    """
    store = _store()
    if not store or store not in ("tomball", "copperfield"):
        return jsonify({"ok": False,
                        "error": "Select a specific store (Tomball or Copperfield)."}), 400
    from app.services.toast_employee_profiles import reconcile_toast_employee_profiles
    summary = reconcile_toast_employee_profiles(only_store=store)
    return jsonify({"ok": True, "store": store, "profiles": summary}), 200


def toast_employee_summary(store: str, toast_id: str) -> tuple[dict, int]:
    """Resolve ONE Toast employee's recent labor + performance + (derived) pay
    for `store`, by Toast GUID. SHARED by the manager Link-tab endpoint and the
    employee self view (/employee/my-performance) so both surface IDENTICAL
    numbers. Pure data -- no request/permission context; the caller gates.

    Resolve location + GUID; pull fetch_time_entries over the last ~30 days and
    keep only THIS employee's entries (employeeReference.guid == toast_id) ->
    hours (total) + timecards (in/out). Then server_perf_report for the same
    window + store, matched by name -> performance. payroll is DERIVED from the
    entries x hourlyWage IF Toast exposes a rate ({estimated:true}); otherwise we
    say the Payroll API isn't wired (pending creds) rather than fabricate.

    Returns (payload, http_status). Any Toast/creds failure ->
    ({ok:False, error}, 502) -- never raises, never a 500. Sam #2629 / #2829.
    """
    if not store or store not in ("tomball", "copperfield"):
        return {"ok": False,
                "error": "Select a specific store (Tomball or Copperfield)."}, 400

    guids = restaurant_guids()
    guid = guids.get(store)
    if not guid:
        return {"ok": False,
                "error": f"No Toast restaurant GUID configured for {store}."}, 502

    # Last ~30 days, inclusive (CT "today" offset, matching toast_client).
    end = datetime.utcnow() - timedelta(hours=5)
    start = end - timedelta(days=_TIMECARD_WINDOW_DAYS)

    client = ToastClient.shared()

    # --- time entries -> hours + timecards (filtered to THIS toast_id) ---
    try:
        entries = client.fetch_time_entries(store, guid, start, end) or []
    except Exception as ex:
        log.warning("toast-link: fetch_time_entries failed for %s/%s: %s", store, toast_id, ex)
        return {"ok": False, "error": f"Toast time entries unavailable: {ex}"}, 502

    timecards: list[dict] = []
    total_hours = 0.0
    total_cost = 0.0
    wage_seen = False
    for te in entries:
        if te.get("deleted"):
            continue
        if (te.get("employeeReference") or {}).get("guid") != toast_id:
            continue
        reg = float(te.get("regularHours") or 0)
        ot = float(te.get("overtimeHours") or 0)
        hrs = reg + ot
        total_hours += hrs
        wage = te.get("hourlyWage")
        if wage is not None:
            wage_seen = True
            w = float(wage or 0)
            total_cost += reg * w + ot * w * 1.5
        timecards.append({
            "guid": te.get("guid"),
            "in": te.get("inDate"),
            "out": te.get("outDate"),
            "regular_hours": reg,
            "overtime_hours": ot,
            "hours": hrs,
            "open": te.get("outDate") in (None, "") or (te.get("outDate") or "").startswith("1970"),
        })
    timecards.sort(key=lambda t: (t["in"] or ""))

    # --- performance: the Operations Performance pull, matched by name ---
    performance: dict = {"available": False,
                         "note": "No Toast server-performance data for this employee in the window."}
    toast_name = ""
    try:
        # Resolve this employee's display name (to match the perf row by name).
        for e in (client.fetch_employees(store, guid) or []):
            if e.get("guid") == toast_id:
                toast_name = _toast_name(e)
                break
        from app.services import toast_reports
        report = toast_reports.server_perf_report(start, end, store)
        rows = ((report.get("by_location") or {}).get(store) or {}).get("rows") or []
        want = _norm_full(toast_name)
        match_row = next((r for r in rows if _norm_full(r.get("name") or "") == want), None)
        if match_row:
            performance = {"available": True, **match_row}
    except Exception as ex:
        # Performance is best-effort: a failure here must not 500 or blank the
        # whole response (hours/timecards are still useful). Surface a note.
        log.warning("toast-link: server_perf_report failed for %s/%s: %s", store, toast_id, ex)
        performance = {"available": False, "note": f"Performance unavailable: {ex}"}

    # --- payroll: derive from time entries x wage IF a rate is available ---
    if wage_seen and total_hours > 0:
        payroll = {
            "available": True,
            "estimated": True,
            "gross_pay": round(total_cost, 2),
            "hours": round(total_hours, 2),
            "note": "Estimated from Toast time entries x hourlyWage (reg + 1.5x OT); "
                    "not the official Toast Payroll figure.",
        }
    else:
        payroll = {
            "available": False,
            "note": "Toast Payroll API not wired -- pending creds check",
        }

    return {
        "ok": True,
        "hours": round(total_hours, 2),
        "timecards": timecards,
        "performance": performance,
        "payroll": payroll,
    }, 200


@store_bp.route("/schedules-v2/toast/employee/<toast_id>", methods=["GET"])
@require_level(_MGR)
def sv2_toast_employee(toast_id):
    """LINK TAB -- one Toast employee's recent labor + performance for THIS store
    (manager view). Serves from the cached ToastEmployeeSnapshot (refreshed in
    the background by toast_sync -- Sam #2845), so this is a fast DB read with NO
    live Toast pull -> the page can't 502 and never shows a raw Toast error.
    Not synced yet / last pull failed -> a clean {syncing:true} state. The actual
    Toast pull (toast_employee_summary) now runs only in the background sync."""
    from app.services.toast_sync import read_snapshot
    snap = read_snapshot(_store(), toast_id)
    if snap is None:
        return jsonify({"ok": True, "syncing": True, "hours": 0, "timecards": [],
                        "performance": {"available": False,
                                        "note": "Syncing from Toast -- check back shortly."},
                        "payroll": {"available": False, "note": "Syncing from Toast..."}}), 200
    if not snap.get("ok"):
        return jsonify({"ok": True, "syncing": True, "hours": snap.get("hours") or 0,
                        "timecards": snap.get("timecards") or [],
                        "performance": {"available": False,
                                        "note": "Toast sync pending -- retrying automatically."},
                        "payroll": {"available": False, "note": "Toast sync pending."},
                        "synced_at": snap.get("synced_at")}), 200
    return jsonify({"ok": True, "syncing": False, "hours": snap.get("hours") or 0,
                    "timecards": snap.get("timecards") or [],
                    "performance": snap.get("performance") or {"available": False},
                    "payroll": snap.get("payroll") or {"available": False},
                    "synced_at": snap.get("synced_at")}), 200
