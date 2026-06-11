"""Floor / Sections JSON API + page routes (docs/floor_contract.md sections 6+7).

SA-2 lane. Blueprint `floor` is fully self-contained: the orchestrator
registers it in app/__init__.py at Gate 2; until then local verification is
`from app import create_app; app = create_app(); app.register_blueprint(floor_bp)`.

Design rules honored here (see the contract):
- All DB state, no process-local state (gunicorn multi-worker).
- All DateTime columns are naive UTC; "business date" = local date in APP_TZ
  (America/Chicago), same approach as app.models._local_today.
- Rows are keyed by location_guid (Toast restaurant GUID); slugs/keys are
  resolution + display only. Joins are table GUID + employee GUID.
- Toast is READ-ONLY (GET) - this module never writes to Toast.
- Response envelope: success {"ok": true, ...}; failure
  {"ok": false, "error": "<code>"} with a proper HTTP status.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, abort, g, jsonify, render_template, request

from app.floor_models import (
    FIXTURE_TYPES,
    FLOOR_PALETTE,
    RESERVATION_STATUSES,
    TABLE_SHAPES,
    WAITLIST_STATUSES,
    FloorFixture,
    FloorLayout,
    FloorReservation,
    FloorSeating,
    FloorSection,
    FloorSectionTable,
    FloorWaitlistEntry,
    ToastServiceArea,
    ToastTableCfg,
    ensure_floor_tables,
)
from app.models import _local_today
from app.web.dashboard_access import (
    current_role_is,
    has_dashboard_access,
    require_dashboard_access,
)

log = logging.getLogger(__name__)

floor_bp = Blueprint("floor", __name__, url_prefix="/floor")

# Boot-time schema apply (contract section 3): prod has no alembic step, so
# the floor tables are created here, at import time, via checkfirst
# create_all. engine may be None (no DATABASE_URL) - ensure_floor_tables
# handles that; any other failure is non-fatal so the app still boots.
try:
    from app.db import engine as _boot_engine
    ensure_floor_tables(_boot_engine)
except Exception:
    log.exception("floor: boot-time ensure_floor_tables failed (non-fatal)")


# ---------------------------------------------------------------------------
# Location resolution (contract section 1)
# ---------------------------------------------------------------------------

_LOC_BY_SLUG = {
    "uno": {"slug": "uno", "key": "copperfield", "label": "Copperfield"},
    "dos": {"slug": "dos", "key": "tomball", "label": "Tomball"},
}
_PAGE_SLUGS = ("uno", "dos", "partner", "corporate")

PARTY_SIZE_MIN = 1
PARTY_SIZE_MAX = 30


def resolve_loc(slug: str | None) -> dict | None:
    """uno -> copperfield, dos -> tomball; GUID via toast_client
    restaurant_guids(). Returns {slug,key,guid,label} or None when the slug
    is unknown (routes answer 400 JSON for None)."""
    base = _LOC_BY_SLUG.get((slug or "").strip().lower())
    if base is None:
        return None
    out = dict(base)
    guid = ""
    try:
        from app.services.toast_client import restaurant_guids
        guid = restaurant_guids().get(out["key"]) or ""
    except Exception:
        log.exception("floor: restaurant_guids() failed")
    if not guid:
        # Env GUID missing (local dev without Toast env). Keep the two
        # stores' keyspaces apart with a deterministic per-store fallback.
        guid = f"unset-{out['key']}"
        log.warning("floor: no Toast restaurant GUID for %s; using %s",
                    out["key"], guid)
    out["guid"] = guid
    return out


def _loc_or_400():
    """Resolve loc from ?loc= (or the JSON body for POSTs). Returns
    (loc_dict, None) or (None, 400-response)."""
    slug = request.args.get("loc")
    if not slug and request.method in ("POST", "PUT", "PATCH"):
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            slug = body.get("loc")
    loc = resolve_loc(slug)
    if loc is None:
        return None, (jsonify({"ok": False, "error": "unknown_loc"}), 400)
    return loc, None


# ---------------------------------------------------------------------------
# Auth (contract section 6)
# ---------------------------------------------------------------------------

def _is_manager() -> bool:
    """Manager = dash.operations AND NOT role expo (the
    _operations_full_access_ok pattern)."""
    return has_dashboard_access("dash.operations") and not current_role_is("expo")


@floor_bp.before_request
def _floor_api_gate():
    """ALL /floor/api/* require dash.operations. JSON 403, never a
    redirect. The page route gates itself (it needs the explicit
    store_slug)."""
    path = request.path or ""
    if path.startswith("/floor/api/"):
        if not has_dashboard_access("dash.operations"):
            return jsonify({"ok": False, "error": "forbidden"}), 403
    return None


def _manager_or_403():
    if not _is_manager():
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return None


def _user_display_name() -> str:
    u = getattr(g, "current_user", None)
    return (getattr(u, "full_name", "") or "") if u is not None else ""


# ---------------------------------------------------------------------------
# DB session
# ---------------------------------------------------------------------------

def _open_db():
    """Late-bound SessionLocal so tests can monkeypatch app.db.SessionLocal
    (same seam the dashboard-access tests use)."""
    from app.db import SessionLocal
    if SessionLocal is None:
        abort(503)
    return SessionLocal()


# ---------------------------------------------------------------------------
# Time helpers (contract section 2)
# ---------------------------------------------------------------------------

def _fallback_offset_hours(d: date) -> int:
    """US central offset without tzdata - mirrors app.models._local_today's
    fallback (CDT -5 between the 2nd Sunday of March and the 1st Sunday of
    November, else CST -6)."""
    y = d.year
    mar1 = date(y, 3, 1)
    second_sunday_march = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1 = date(y, 11, 1)
    first_sunday_nov = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return -5 if second_sunday_march <= d < first_sunday_nov else -6


def _app_tz():
    from zoneinfo import ZoneInfo
    return ZoneInfo(os.getenv("APP_TZ", "America/Chicago"))


def _utc_window_for_business_date(d: date) -> tuple[datetime, datetime]:
    """[start, end) in naive UTC covering local business date `d`."""
    nd = d + timedelta(days=1)
    try:
        tz = _app_tz()
        start = datetime(d.year, d.month, d.day, tzinfo=tz)
        end = datetime(nd.year, nd.month, nd.day, tzinfo=tz)
        return (
            start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None),
        )
    except Exception:
        return (
            datetime(d.year, d.month, d.day) - timedelta(hours=_fallback_offset_hours(d)),
            datetime(nd.year, nd.month, nd.day) - timedelta(hours=_fallback_offset_hours(nd)),
        )


def _parse_date_or_none(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return None


def _date_param_or_today():
    """Returns (date, None) or (None, 400-response)."""
    raw = request.args.get("date")
    if not raw:
        return _local_today(), None
    d = _parse_date_or_none(raw)
    if d is None:
        return None, (jsonify({"ok": False, "error": "bad_date"}), 400)
    return d, None


def _parse_dt_to_utc(raw) -> datetime | None:
    """ISO-8601 datetime -> naive UTC. Naive input = APP_TZ local
    (contract endpoint 12); aware input is converted."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=_app_tz())
        except Exception:
            return dt - timedelta(hours=_fallback_offset_hours(dt.date()))
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _iso_z(dt: datetime | None) -> str | None:
    """Naive-UTC datetime -> ISO-8601 'Z' string (contract serialization)."""
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + "Z"


def _utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


# ---------------------------------------------------------------------------
# Toast employee helpers (contract section 5; READ-ONLY GETs, never writes)
# ---------------------------------------------------------------------------

def initials_of(name: str) -> str:
    """First letter of the first + first letter of the last word, upper."""
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _toast_display_name(e: dict) -> str:
    first = (e.get("firstName") or e.get("chosenName") or "").strip()
    last = (e.get("lastName") or "").strip()
    return (f"{first} {last}".strip()
            or e.get("email") or (e.get("guid") or "?")[:8])


def _fetch_toast_employees(loc: dict) -> list[dict]:
    """[{employee_guid,name,initials}], deleted excluded. Raises on Toast
    failure (callers decide between 502 and display-only fallback)."""
    from app.services.toast_client import ToastClient
    rows = ToastClient.shared().fetch_employees(loc["key"], loc["guid"]) or []
    out = []
    for e in rows:
        if not isinstance(e, dict) or e.get("deleted"):
            continue
        guid = e.get("guid")
        if not guid:
            continue
        name = _toast_display_name(e)
        out.append({"employee_guid": guid, "name": name,
                    "initials": initials_of(name)})
    return out


def _employee_name_map(loc: dict) -> dict[str, str]:
    """guid -> display name; {} when Toast is unavailable (names are
    display-only, so listing endpoints degrade instead of failing)."""
    try:
        return {e["employee_guid"]: e["name"] for e in _fetch_toast_employees(loc)}
    except Exception:
        log.warning("floor: employee name lookup unavailable for %s", loc["key"])
        return {}


def _name_or_guid(name_map: dict[str, str], guid: str | None) -> str:
    if not guid:
        return ""
    return name_map.get(guid) or guid[:8]


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def _json_body_or_400():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return None, (jsonify({"ok": False, "error": "bad_json"}), 400)
    return body, None


def _valid_party_size(v) -> int | None:
    """int 1..30 or None (contract: party_size validated 1..30)."""
    if isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if PARTY_SIZE_MIN <= n <= PARTY_SIZE_MAX:
        return n
    return None


def _attention_minutes() -> int:
    try:
        return int(os.getenv("FLOOR_ATTENTION_MINUTES", "90"))
    except ValueError:
        return 90


# Gate 4 (ck): no-show grace window (contract sections 2 + 12).
def _noshow_grace_minutes() -> int:
    try:
        return int(os.getenv("FLOOR_NOSHOW_GRACE_MINUTES", "20"))
    except ValueError:
        return 20


def _apply_noshow_flags(db, loc: dict) -> None:
    """Gate 4 (ck): lazy no-show auto-flag (contract section 12). Any
    reservation at this location still upcoming/confirmed whose
    reserved_for + grace < now flips to no_show, persisted on read inside
    GET reservations / history / live. No cron, no in-memory state: a plain
    idempotent UPDATE, safe when several gunicorn workers race it."""
    cutoff = _utcnow() - timedelta(minutes=_noshow_grace_minutes())
    flipped = (
        db.query(FloorReservation)
        .filter(FloorReservation.location_guid == loc["guid"],
                FloorReservation.status.in_(["upcoming", "confirmed"]),
                FloorReservation.reserved_for < cutoff)
        .update({"status": "no_show"}, synchronize_session=False)
    )
    if flipped:
        db.commit()
        # Drop any stale identity-map copies of the rows we just flipped.
        db.expire_all()


def _serialize_reservation(r: FloorReservation, *, with_notes: bool = True) -> dict:
    out = {
        "id": r.id,
        "guest_name": r.guest_name,
        "phone": r.phone or "",
        "party_size": r.party_size,
        "reserved_for": _iso_z(r.reserved_for),
        "status": r.status,
        "seating_id": r.seating_id,
    }
    if with_notes:
        out["notes"] = r.notes or ""
    return out


def _serialize_waitlist(w: FloorWaitlistEntry) -> dict:
    return {
        "id": w.id,
        "guest_name": w.guest_name,
        "phone": w.phone or "",
        "party_size": w.party_size,
        "quoted_minutes": w.quoted_minutes,
        "joined_at": _iso_z(w.joined_at),
        "status": w.status,
        "seating_id": w.seating_id,
    }


def _sections_payload(db, loc: dict, d: date) -> list[dict]:
    name_map = _employee_name_map(loc)
    secs = (
        db.query(FloorSection)
        .filter(FloorSection.location_guid == loc["guid"],
                FloorSection.shift_date == d)
        .order_by(FloorSection.id)
        .all()
    )
    sec_ids = [s.id for s in secs]
    tables_by_sec: dict[int, list[str]] = {sid: [] for sid in sec_ids}
    if sec_ids:
        for st in (db.query(FloorSectionTable)
                   .filter(FloorSectionTable.section_id.in_(sec_ids))
                   .order_by(FloorSectionTable.table_guid)
                   .all()):
            tables_by_sec.setdefault(st.section_id, []).append(st.table_guid)
    out = []
    for s in secs:
        name = _name_or_guid(name_map, s.server_employee_guid)
        out.append({
            "id": s.id,
            "server_employee_guid": s.server_employee_guid,
            "server_name": name,
            "initials": initials_of(name),
            "color": s.color,
            "table_guids": tables_by_sec.get(s.id, []),
        })
    return out


# ===========================================================================
# Page route (contract section 7)
# ===========================================================================

_TAB_TEMPLATES = {
    "assign": "sections_assign.html",
    "host": "sections_host.html",
    "map": "sections_map.html",
}


def _reachable_locations() -> list[dict]:
    """[{slug,key,label}] of uno/dos the user can reach - same
    accessible_store_slugs logic as team_workspace. partner/corporate ->
    both; default data location is uno."""
    from app.web.permissions import accessible_store_slugs
    acc = accessible_store_slugs(getattr(g, "current_user", None))
    if ("partner" in acc) or ("corporate" in acc):
        slugs = ["uno", "dos"]
    else:
        slugs = [s for s in ("uno", "dos") if s in acc]
    if not slugs:
        # Legacy/tooling session with no resolvable scope: offer both, the
        # per-request gates still guard the data (team_workspace pattern).
        slugs = ["uno", "dos"]
    return [
        {"slug": s, "key": _LOC_BY_SLUG[s]["key"], "label": _LOC_BY_SLUG[s]["label"]}
        for s in slugs
    ]


@floor_bp.route("/<store_slug>/sections", methods=["GET"])
def sections_page(store_slug: str):
    slug = (store_slug or "").strip().lower()
    if slug not in _PAGE_SLUGS:
        abort(404)
    require_dashboard_access("dash.operations", slug)
    tab = (request.args.get("tab") or "assign").strip().lower()
    if tab not in _TAB_TEMPLATES:
        tab = "assign"
    is_manager = _is_manager()
    if tab == "map" and not is_manager:
        abort(403)
    locations = _reachable_locations()
    loc_default = "uno" if any(l["slug"] == "uno" for l in locations) else locations[0]["slug"]
    return render_template(
        _TAB_TEMPLATES[tab],
        store_slug=slug,
        active_tab=tab,
        locations_json=json.dumps(locations),
        loc_default=loc_default,
        is_manager=is_manager,
        attention_minutes=_attention_minutes(),
        user_name=_user_display_name(),
    )


# ===========================================================================
# JSON API (contract section 6)
# ===========================================================================

# --- 1. GET /floor/api/floor ----------------------------------------------

def _bootstrap_sync_if_empty(db, loc) -> None:
    """Gate-2 integration (ck): first-touch bootstrap. A fresh deploy has no
    synced Toast config rows and no manager-facing sync button, so the first
    GET /floor/api/floor for a location with ZERO toast_tables rows triggers
    one read-only config sync in-request (a few seconds, once per location
    ever). Best-effort: any failure leaves the response as an honest empty
    floor. Concurrent workers racing here is safe - upserts are by guid and
    idempotent. Manual refresh stays POST /floor/api/sync (manager)."""
    has_any = (
        db.query(ToastTableCfg.guid)
        .filter(ToastTableCfg.location_guid == loc["guid"])
        .first()
    )
    if has_any is not None:
        return
    try:
        from app.services.toast_config_sync import sync_location
        sync_location(loc["key"])
        db.expire_all()
    except Exception:
        log.exception("floor bootstrap sync failed for %s", loc["key"])


@floor_bp.route("/api/floor", methods=["GET"])
def api_floor():
    loc, err = _loc_or_400()
    if err:
        return err
    db = _open_db()
    try:
        _bootstrap_sync_if_empty(db, loc)
        tables = (
            db.query(ToastTableCfg)
            .filter(ToastTableCfg.location_guid == loc["guid"],
                    ToastTableCfg.deleted.is_(False))
            .order_by(ToastTableCfg.name)
            .all()
        )
        layouts = {
            l.table_guid: l
            for l in db.query(FloorLayout)
            .filter(FloorLayout.location_guid == loc["guid"]).all()
        }
        placed, unplaced = [], []
        for t in tables:
            lay = layouts.get(t.guid)
            if lay is None:
                unplaced.append({
                    "guid": t.guid,
                    "name": t.name,
                    "service_area_guid": t.service_area_guid,
                })
            else:
                placed.append({
                    "guid": t.guid,
                    "name": t.name,
                    "service_area_guid": t.service_area_guid,
                    "revenue_center_guid": t.revenue_center_guid,
                    "x": lay.x, "y": lay.y, "w": lay.w, "h": lay.h,
                    "shape": lay.shape, "rotation": lay.rotation,
                })
        fixtures = [
            {"id": f.id, "type": f.type, "x": f.x, "y": f.y, "w": f.w,
             "h": f.h, "rotation": f.rotation, "label": f.label}
            for f in db.query(FloorFixture)
            .filter(FloorFixture.location_guid == loc["guid"])
            .order_by(FloorFixture.id).all()
        ]
        areas = [
            {"guid": a.guid, "name": a.name}
            for a in db.query(ToastServiceArea)
            .filter(ToastServiceArea.location_guid == loc["guid"],
                    ToastServiceArea.deleted.is_(False))
            .order_by(ToastServiceArea.name).all()
        ]
        return jsonify({
            "ok": True,
            "location": loc,
            "service_areas": areas,
            "tables": placed,
            "unplaced": unplaced,
            "fixtures": fixtures,
        })
    finally:
        db.close()


# --- 2. PUT /floor/api/layout (manager) ------------------------------------

@floor_bp.route("/api/layout", methods=["PUT"])
def api_layout_put():
    deny = _manager_or_403()
    if deny:
        return deny
    loc, err = _loc_or_400()
    if err:
        return err
    body, err = _json_body_or_400()
    if err:
        return err
    items = body.get("tables")
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "bad_layout"}), 400
    rows = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            return jsonify({"ok": False, "error": "bad_layout"}), 400
        guid = (it.get("table_guid") or "").strip()
        if not guid or guid in seen:
            return jsonify({"ok": False, "error": "bad_layout"}), 400
        seen.add(guid)
        shape = (it.get("shape") or "square").strip().lower()
        if shape not in TABLE_SHAPES:
            return jsonify({"ok": False, "error": "bad_shape"}), 400
        try:
            rows.append(FloorLayout(
                location_guid=loc["guid"],
                table_guid=guid,
                x=float(it.get("x", 0.0)),
                y=float(it.get("y", 0.0)),
                w=float(it.get("w", 80.0)),
                h=float(it.get("h", 80.0)),
                shape=shape,
                rotation=int(it.get("rotation", 0)),
            ))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad_layout"}), 400
    db = _open_db()
    try:
        db.query(FloorLayout).filter(
            FloorLayout.location_guid == loc["guid"]).delete()
        db.add_all(rows)
        db.commit()
        return jsonify({"ok": True, "count": len(rows)})
    finally:
        db.close()


# --- 3. PUT /floor/api/fixtures (manager) -----------------------------------

@floor_bp.route("/api/fixtures", methods=["PUT"])
def api_fixtures_put():
    deny = _manager_or_403()
    if deny:
        return deny
    loc, err = _loc_or_400()
    if err:
        return err
    body, err = _json_body_or_400()
    if err:
        return err
    items = body.get("fixtures")
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "bad_fixtures"}), 400
    rows = []
    for it in items:
        if not isinstance(it, dict):
            return jsonify({"ok": False, "error": "bad_fixtures"}), 400
        ftype = (it.get("type") or "").strip().lower()
        if ftype not in FIXTURE_TYPES:
            return jsonify({"ok": False, "error": "bad_fixture_type"}), 400
        label = it.get("label")
        if label is not None and not isinstance(label, str):
            return jsonify({"ok": False, "error": "bad_fixtures"}), 400
        try:
            rows.append(FloorFixture(
                location_guid=loc["guid"],
                type=ftype,
                x=float(it.get("x", 0.0)),
                y=float(it.get("y", 0.0)),
                w=float(it.get("w", 120.0)),
                h=float(it.get("h", 20.0)),
                rotation=int(it.get("rotation", 0)),
                label=(label or None) and label[:60],
            ))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad_fixtures"}), 400
    db = _open_db()
    try:
        db.query(FloorFixture).filter(
            FloorFixture.location_guid == loc["guid"]).delete()
        db.add_all(rows)
        db.commit()
        return jsonify({"ok": True, "count": len(rows)})
    finally:
        db.close()


# --- 4. GET /floor/api/sections ---------------------------------------------

@floor_bp.route("/api/sections", methods=["GET"])
def api_sections_get():
    loc, err = _loc_or_400()
    if err:
        return err
    d, err = _date_param_or_today()
    if err:
        return err
    db = _open_db()
    try:
        return jsonify({
            "ok": True,
            "date": d.isoformat(),
            "sections": _sections_payload(db, loc, d),
        })
    finally:
        db.close()


# --- 5. POST /floor/api/sections (manager) ----------------------------------

@floor_bp.route("/api/sections", methods=["POST"])
def api_sections_post():
    deny = _manager_or_403()
    if deny:
        return deny
    loc, err = _loc_or_400()
    if err:
        return err
    body, err = _json_body_or_400()
    if err:
        return err
    raw_date = body.get("date")
    if raw_date:
        d = _parse_date_or_none(raw_date)
        if d is None:
            return jsonify({"ok": False, "error": "bad_date"}), 400
    else:
        d = _local_today()
    confirm = bool(body.get("confirm"))
    items = body.get("sections")
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "bad_sections"}), 400

    # Validate + collect before any write.
    parsed = []
    server_guids: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            return jsonify({"ok": False, "error": "bad_sections"}), 400
        sg = (it.get("server_employee_guid") or "").strip()
        if not sg:
            return jsonify({"ok": False, "error": "bad_server_guid"}), 400
        if sg in server_guids:
            return jsonify({"ok": False, "error": "duplicate_server"}), 400
        server_guids.add(sg)
        color = it.get("color")
        if color is not None:
            if not isinstance(color, str) or not color.strip() or len(color.strip()) > 10:
                return jsonify({"ok": False, "error": "bad_color"}), 400
            color = color.strip()
        tgs = it.get("table_guids", [])
        if not isinstance(tgs, list) or any(
                not isinstance(t, str) or not t.strip() for t in tgs):
            return jsonify({"ok": False, "error": "bad_table_guids"}), 400
        # dedupe while preserving order (PK is section_id+table_guid)
        deduped = list(dict.fromkeys(t.strip() for t in tgs))
        parsed.append({"server": sg, "color": color, "tables": deduped})

    db = _open_db()
    try:
        existing = (
            db.query(FloorSection)
            .filter(FloorSection.location_guid == loc["guid"],
                    FloorSection.shift_date == d)
            .all()
        )
        if existing and not confirm:
            return jsonify({"ok": False, "error": "exists", "exists": True}), 409

        # Full replace of the shift's assignment set.
        if existing:
            ex_ids = [s.id for s in existing]
            db.query(FloorSectionTable).filter(
                FloorSectionTable.section_id.in_(ex_ids)).delete(
                synchronize_session=False)
            db.query(FloorSection).filter(
                FloorSection.id.in_(ex_ids)).delete(synchronize_session=False)

        # Palette auto-assign (contract section 5): first unused hex in this
        # (loc, date); wrap by index once all 8 are used.
        used = {p["color"] for p in parsed if p["color"]}
        palette_hexes = [c["hex"] for c in FLOOR_PALETTE]
        for i, p in enumerate(parsed):
            if p["color"]:
                continue
            free = [h for h in palette_hexes if h not in used]
            p["color"] = free[0] if free else palette_hexes[i % len(palette_hexes)]
            used.add(p["color"])

        created_by = _user_display_name()
        now = _utcnow()
        for p in parsed:
            sec = FloorSection(
                location_guid=loc["guid"],
                shift_date=d,
                server_employee_guid=p["server"],
                color=p["color"],
                created_by=created_by,
                created_at=now,
            )
            db.add(sec)
            db.flush()
            for tg in p["tables"]:
                db.add(FloorSectionTable(section_id=sec.id, table_guid=tg))
        db.commit()
        return jsonify({
            "ok": True,
            "date": d.isoformat(),
            "sections": _sections_payload(db, loc, d),
        })
    finally:
        db.close()


# --- 6. GET /floor/api/employees ---------------------------------------------

@floor_bp.route("/api/employees", methods=["GET"])
def api_employees():
    loc, err = _loc_or_400()
    if err:
        return err
    try:
        employees = _fetch_toast_employees(loc)
    except Exception:
        log.warning("floor: Toast employees unavailable for %s", loc["key"])
        return jsonify({"ok": False, "error": "toast_unavailable"}), 502
    return jsonify({"ok": True, "employees": employees})


# --- 7. GET /floor/api/employees-on-shift ------------------------------------

@floor_bp.route("/api/employees-on-shift", methods=["GET"])
def api_employees_on_shift():
    loc, err = _loc_or_400()
    if err:
        return err
    d, err = _date_param_or_today()
    if err:
        return err

    shift_guids: list[str] = []
    try:
        from app.services.toast_client import ToastClient
        day = datetime(d.year, d.month, d.day)
        shifts = ToastClient.shared().fetch_shifts(
            loc["key"], loc["guid"], day, day) or []
        for s in shifts:
            if not isinstance(s, dict) or s.get("deleted"):
                continue
            eg = ((s.get("employeeReference") or {}).get("guid")) or ""
            if eg and eg not in shift_guids:
                shift_guids.append(eg)
    except Exception:
        log.warning("floor: fetch_shifts unavailable for %s %s", loc["key"], d)
        shift_guids = []

    if shift_guids:
        source = "shifts"
        name_map = _employee_name_map(loc)
        base = [
            {"employee_guid": gd,
             "name": _name_or_guid(name_map, gd),
             "initials": initials_of(_name_or_guid(name_map, gd))}
            for gd in shift_guids
        ]
    else:
        source = "employees"
        try:
            base = _fetch_toast_employees(loc)
        except Exception:
            log.warning("floor: Toast employees unavailable for %s", loc["key"])
            return jsonify({"ok": False, "error": "toast_unavailable"}), 502

    palette_hexes = [c["hex"] for c in FLOOR_PALETTE]
    servers = []
    for i, e in enumerate(base):
        servers.append({
            "employee_guid": e["employee_guid"],
            "name": e["name"],
            "initials": e["initials"],
            "color": palette_hexes[i % len(palette_hexes)],
        })
    return jsonify({"ok": True, "source": source, "servers": servers})


# --- 8. GET /floor/api/live ----------------------------------------------------

@floor_bp.route("/api/live", methods=["GET"])
def api_live():
    loc, err = _loc_or_400()
    if err:
        return err
    db = _open_db()
    try:
        # Gate 4 (ck): lazy no-show flag runs on every live read.
        _apply_noshow_flags(db, loc)
        now = datetime.utcnow()
        open_rows = (
            db.query(FloorSeating)
            .filter(FloorSeating.location_guid == loc["guid"],
                    FloorSeating.cleared_at.is_(None))
            .order_by(FloorSeating.seated_at)
            .all()
        )
        start, end = _utc_window_for_business_date(_local_today())
        today_rows = (
            db.query(FloorSeating)
            .filter(FloorSeating.location_guid == loc["guid"],
                    FloorSeating.seated_at >= start,
                    FloorSeating.seated_at < end)
            .all()
        )
        covers: dict[str, dict] = {}
        for r in open_rows:
            sg = r.server_employee_guid_at_seat
            if sg:
                covers.setdefault(sg, {"live": 0, "today": 0})
                covers[sg]["live"] += r.party_size or 0
        for r in today_rows:
            sg = r.server_employee_guid_at_seat
            if sg:
                covers.setdefault(sg, {"live": 0, "today": 0})
                covers[sg]["today"] += r.party_size or 0
        open_out = [
            {
                "seating_id": r.id,
                "table_guid": r.table_guid,
                "party_size": r.party_size,
                "seated_at": _iso_z(r.seated_at),
                "minutes": max(0, int((now - r.seated_at).total_seconds() // 60)),
                "server_employee_guid": r.server_employee_guid_at_seat,
                "reservation_id": r.reservation_id,
                "waitlist_id": r.waitlist_id,
            }
            for r in open_rows
        ]
        # Gate 4 (ck): host-tab badge = today's reservations still in play
        # (upcoming/confirmed/arrived) for this location (contract section 12).
        reservation_badge = (
            db.query(FloorReservation.id)
            .filter(FloorReservation.location_guid == loc["guid"],
                    FloorReservation.reserved_for >= start,
                    FloorReservation.reserved_for < end,
                    FloorReservation.status.in_(
                        ["upcoming", "confirmed", "arrived"]))
            .count()
        )
        return jsonify({
            "ok": True,
            "attention_minutes": _attention_minutes(),
            "open": open_out,
            "covers": covers,
            "reservation_badge": reservation_badge,
        })
    finally:
        db.close()


# --- 9. POST /floor/api/seat ----------------------------------------------------

@floor_bp.route("/api/seat", methods=["POST"])
def api_seat():
    loc, err = _loc_or_400()
    if err:
        return err
    body, err = _json_body_or_400()
    if err:
        return err
    table_guid = (body.get("table_guid") or "").strip()
    if not table_guid:
        return jsonify({"ok": False, "error": "missing_table_guid"}), 400

    db = _open_db()
    try:
        open_existing = (
            db.query(FloorSeating)
            .filter(FloorSeating.location_guid == loc["guid"],
                    FloorSeating.table_guid == table_guid,
                    FloorSeating.cleared_at.is_(None))
            .first()
        )
        if open_existing is not None:
            return jsonify({"ok": False, "error": "occupied"}), 409

        reservation = None
        if body.get("reservation_id") is not None:
            try:
                rid = int(body["reservation_id"])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "bad_reservation_id"}), 400
            reservation = (
                db.query(FloorReservation)
                .filter(FloorReservation.id == rid,
                        FloorReservation.location_guid == loc["guid"])
                .first()
            )
            if reservation is None:
                return jsonify({"ok": False, "error": "reservation_not_found"}), 404

        waitlist_entry = None
        if body.get("waitlist_id") is not None:
            try:
                wid = int(body["waitlist_id"])
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "bad_waitlist_id"}), 400
            waitlist_entry = (
                db.query(FloorWaitlistEntry)
                .filter(FloorWaitlistEntry.id == wid,
                        FloorWaitlistEntry.location_guid == loc["guid"])
                .first()
            )
            if waitlist_entry is None:
                return jsonify({"ok": False, "error": "waitlist_not_found"}), 404

        # party_size resolution: explicit > linked reservation/waitlist > 400
        if body.get("party_size") is not None:
            party_size = _valid_party_size(body.get("party_size"))
            if party_size is None:
                return jsonify({"ok": False, "error": "bad_party_size"}), 400
        elif reservation is not None:
            party_size = reservation.party_size
        elif waitlist_entry is not None:
            party_size = waitlist_entry.party_size
        else:
            return jsonify({"ok": False, "error": "party_size_required"}), 400

        # server resolution: explicit > today's section containing the table > NULL
        server_guid = (body.get("server_employee_guid") or "").strip() or None
        if server_guid is None:
            sec = (
                db.query(FloorSection)
                .join(FloorSectionTable,
                      FloorSectionTable.section_id == FloorSection.id)
                .filter(FloorSection.location_guid == loc["guid"],
                        FloorSection.shift_date == _local_today(),
                        FloorSectionTable.table_guid == table_guid)
                .first()
            )
            server_guid = sec.server_employee_guid if sec is not None else None

        seating = FloorSeating(
            location_guid=loc["guid"],
            table_guid=table_guid,
            party_size=party_size,
            seated_at=_utcnow(),
            seated_by=_user_display_name(),
            server_employee_guid_at_seat=server_guid,
            reservation_id=reservation.id if reservation is not None else None,
            waitlist_id=waitlist_entry.id if waitlist_entry is not None else None,
        )
        db.add(seating)
        db.flush()
        # Link back (contract endpoint 9)
        if reservation is not None:
            reservation.status = "seated"
            reservation.seating_id = seating.id
        if waitlist_entry is not None:
            waitlist_entry.status = "seated"
            waitlist_entry.seating_id = seating.id
        db.commit()
        return jsonify({
            "ok": True,
            "seating_id": seating.id,
            "seating": {
                "seating_id": seating.id,
                "table_guid": seating.table_guid,
                "party_size": seating.party_size,
                "seated_at": _iso_z(seating.seated_at),
                "server_employee_guid": seating.server_employee_guid_at_seat,
                "reservation_id": seating.reservation_id,
                "waitlist_id": seating.waitlist_id,
            },
        })
    finally:
        db.close()


# --- 10. POST /floor/api/clear ---------------------------------------------------

@floor_bp.route("/api/clear", methods=["POST"])
def api_clear():
    loc, err = _loc_or_400()
    if err:
        return err
    body, err = _json_body_or_400()
    if err:
        return err
    table_guid = (body.get("table_guid") or "").strip()
    if not table_guid:
        return jsonify({"ok": False, "error": "missing_table_guid"}), 400
    db = _open_db()
    try:
        seating = (
            db.query(FloorSeating)
            .filter(FloorSeating.location_guid == loc["guid"],
                    FloorSeating.table_guid == table_guid,
                    FloorSeating.cleared_at.is_(None))
            .first()
        )
        if seating is None:
            return jsonify({"ok": False, "error": "no_open_seating"}), 404
        seating.cleared_at = _utcnow()
        db.commit()
        return jsonify({
            "ok": True,
            "seating_id": seating.id,
            "cleared_at": _iso_z(seating.cleared_at),
        })
    finally:
        db.close()


# --- 11. GET /floor/api/reservations ----------------------------------------------

@floor_bp.route("/api/reservations", methods=["GET"])
def api_reservations_get():
    loc, err = _loc_or_400()
    if err:
        return err
    d, err = _date_param_or_today()
    if err:
        return err
    start, end = _utc_window_for_business_date(d)
    db = _open_db()
    try:
        # Gate 4 (ck): lazy no-show flag runs on every book read.
        _apply_noshow_flags(db, loc)
        rows = (
            db.query(FloorReservation)
            .filter(FloorReservation.location_guid == loc["guid"],
                    FloorReservation.reserved_for >= start,
                    FloorReservation.reserved_for < end)
            .order_by(FloorReservation.reserved_for, FloorReservation.id)
            .all()
        )
        return jsonify({
            "ok": True,
            "date": d.isoformat(),
            "reservations": [_serialize_reservation(r) for r in rows],
        })
    finally:
        db.close()


# --- 12. POST /floor/api/reservations -----------------------------------------------

@floor_bp.route("/api/reservations", methods=["POST"])
def api_reservations_post():
    loc, err = _loc_or_400()
    if err:
        return err
    body, err = _json_body_or_400()
    if err:
        return err
    guest_name = (body.get("guest_name") or "").strip()
    if not guest_name:
        return jsonify({"ok": False, "error": "missing_guest_name"}), 400
    party_size = _valid_party_size(body.get("party_size"))
    if party_size is None:
        return jsonify({"ok": False, "error": "bad_party_size"}), 400
    reserved_for = _parse_dt_to_utc(body.get("reserved_for"))
    if reserved_for is None:
        return jsonify({"ok": False, "error": "bad_reserved_for"}), 400
    phone = body.get("phone")
    if phone is not None and not isinstance(phone, str):
        return jsonify({"ok": False, "error": "bad_phone"}), 400
    notes = body.get("notes")
    if notes is not None and not isinstance(notes, str):
        return jsonify({"ok": False, "error": "bad_notes"}), 400
    db = _open_db()
    try:
        # Gate 4 (ck): duplicate-guest guard (contract section 12). Same loc
        # + same non-empty phone + reserved_for within +/-90 min of an
        # existing NON-cancelled reservation -> 409 unless confirm:true.
        phone_clean = (phone or "").strip()[:40]
        if phone_clean and not bool(body.get("confirm")):
            dup = (
                db.query(FloorReservation.id)
                .filter(FloorReservation.location_guid == loc["guid"],
                        FloorReservation.phone == phone_clean,
                        FloorReservation.status != "cancelled",
                        FloorReservation.reserved_for
                        >= reserved_for - timedelta(minutes=90),
                        FloorReservation.reserved_for
                        <= reserved_for + timedelta(minutes=90))
                .first()
            )
            if dup is not None:
                return jsonify({"ok": False, "error": "duplicate",
                                "duplicate": True}), 409
        r = FloorReservation(
            location_guid=loc["guid"],
            guest_name=guest_name[:120],
            phone=(phone or "").strip()[:40],
            party_size=party_size,
            reserved_for=reserved_for,
            status="upcoming",
            notes=(notes or ""),
            created_by=_user_display_name(),
            created_at=_utcnow(),
        )
        db.add(r)
        db.commit()
        return jsonify({"ok": True, "reservation": _serialize_reservation(r)})
    finally:
        db.close()


# --- 13. PATCH /floor/api/reservations/<id> ------------------------------------------

_RESERVATION_PATCH_FIELDS = (
    "status", "notes", "party_size", "reserved_for", "guest_name", "phone",
)


@floor_bp.route("/api/reservations/<int:res_id>", methods=["PATCH"])
def api_reservations_patch(res_id: int):
    body, err = _json_body_or_400()
    if err:
        return err
    db = _open_db()
    try:
        r = db.query(FloorReservation).filter(
            FloorReservation.id == res_id).first()
        if r is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        # Whitelisted fields only; everything else in the body is ignored.
        if "status" in body:
            status = body.get("status")
            if status not in RESERVATION_STATUSES:
                return jsonify({"ok": False, "error": "bad_status"}), 400
            r.status = status
        if "notes" in body:
            notes = body.get("notes")
            if notes is not None and not isinstance(notes, str):
                return jsonify({"ok": False, "error": "bad_notes"}), 400
            r.notes = notes or ""
        if "party_size" in body:
            ps = _valid_party_size(body.get("party_size"))
            if ps is None:
                return jsonify({"ok": False, "error": "bad_party_size"}), 400
            r.party_size = ps
        if "reserved_for" in body:
            dt = _parse_dt_to_utc(body.get("reserved_for"))
            if dt is None:
                return jsonify({"ok": False, "error": "bad_reserved_for"}), 400
            r.reserved_for = dt
        if "guest_name" in body:
            gn = (body.get("guest_name") or "").strip() \
                if isinstance(body.get("guest_name"), str) else ""
            if not gn:
                return jsonify({"ok": False, "error": "missing_guest_name"}), 400
            r.guest_name = gn[:120]
        if "phone" in body:
            ph = body.get("phone")
            if ph is not None and not isinstance(ph, str):
                return jsonify({"ok": False, "error": "bad_phone"}), 400
            r.phone = (ph or "").strip()[:40]
        db.commit()
        return jsonify({"ok": True, "reservation": _serialize_reservation(r)})
    finally:
        db.close()


# --- 14. GET /floor/api/waitlist -------------------------------------------------------

@floor_bp.route("/api/waitlist", methods=["GET"])
def api_waitlist_get():
    loc, err = _loc_or_400()
    if err:
        return err
    include_done = (request.args.get("include_done") or "0").strip() == "1"
    statuses = ["waiting", "notified"]
    if include_done:
        statuses += ["seated", "left"]
    start, end = _utc_window_for_business_date(_local_today())
    db = _open_db()
    try:
        rows = (
            db.query(FloorWaitlistEntry)
            .filter(FloorWaitlistEntry.location_guid == loc["guid"],
                    FloorWaitlistEntry.joined_at >= start,
                    FloorWaitlistEntry.joined_at < end,
                    FloorWaitlistEntry.status.in_(statuses))
            .order_by(FloorWaitlistEntry.joined_at, FloorWaitlistEntry.id)
            .all()
        )
        return jsonify({
            "ok": True,
            "waitlist": [_serialize_waitlist(w) for w in rows],
        })
    finally:
        db.close()


# --- 15. POST /floor/api/waitlist -------------------------------------------------------

@floor_bp.route("/api/waitlist", methods=["POST"])
def api_waitlist_post():
    loc, err = _loc_or_400()
    if err:
        return err
    body, err = _json_body_or_400()
    if err:
        return err
    guest_name = (body.get("guest_name") or "").strip()
    if not guest_name:
        return jsonify({"ok": False, "error": "missing_guest_name"}), 400
    party_size = _valid_party_size(body.get("party_size"))
    if party_size is None:
        return jsonify({"ok": False, "error": "bad_party_size"}), 400
    phone = body.get("phone")
    if phone is not None and not isinstance(phone, str):
        return jsonify({"ok": False, "error": "bad_phone"}), 400
    quoted = body.get("quoted_minutes")
    if quoted is not None:
        if isinstance(quoted, bool):
            return jsonify({"ok": False, "error": "bad_quoted_minutes"}), 400
        try:
            quoted = int(quoted)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "bad_quoted_minutes"}), 400
        if quoted < 0:
            return jsonify({"ok": False, "error": "bad_quoted_minutes"}), 400
    db = _open_db()
    try:
        w = FloorWaitlistEntry(
            location_guid=loc["guid"],
            guest_name=guest_name[:120],
            phone=(phone or "").strip()[:40],
            party_size=party_size,
            quoted_minutes=quoted,
            joined_at=_utcnow(),
            status="waiting",
        )
        db.add(w)
        db.commit()
        return jsonify({"ok": True, "entry": _serialize_waitlist(w)})
    finally:
        db.close()


# --- 16. PATCH /floor/api/waitlist/<id> ---------------------------------------------------

@floor_bp.route("/api/waitlist/<int:wl_id>", methods=["PATCH"])
def api_waitlist_patch(wl_id: int):
    body, err = _json_body_or_400()
    if err:
        return err
    db = _open_db()
    try:
        w = db.query(FloorWaitlistEntry).filter(
            FloorWaitlistEntry.id == wl_id).first()
        if w is None:
            return jsonify({"ok": False, "error": "not_found"}), 404
        if "status" in body:
            status = body.get("status")
            if status not in WAITLIST_STATUSES:
                return jsonify({"ok": False, "error": "bad_status"}), 400
            # 'notified' is a plain manual status value this run - no SMS /
            # messaging side effects (contract section 3; hook stays clean).
            w.status = status
        if "quoted_minutes" in body:
            quoted = body.get("quoted_minutes")
            if quoted is not None:
                if isinstance(quoted, bool):
                    return jsonify({"ok": False, "error": "bad_quoted_minutes"}), 400
                try:
                    quoted = int(quoted)
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "bad_quoted_minutes"}), 400
                if quoted < 0:
                    return jsonify({"ok": False, "error": "bad_quoted_minutes"}), 400
            w.quoted_minutes = quoted
        if "party_size" in body:
            ps = _valid_party_size(body.get("party_size"))
            if ps is None:
                return jsonify({"ok": False, "error": "bad_party_size"}), 400
            w.party_size = ps
        if "guest_name" in body:
            gn = (body.get("guest_name") or "").strip() \
                if isinstance(body.get("guest_name"), str) else ""
            if not gn:
                return jsonify({"ok": False, "error": "missing_guest_name"}), 400
            w.guest_name = gn[:120]
        if "phone" in body:
            ph = body.get("phone")
            if ph is not None and not isinstance(ph, str):
                return jsonify({"ok": False, "error": "bad_phone"}), 400
            w.phone = (ph or "").strip()[:40]
        db.commit()
        return jsonify({"ok": True, "entry": _serialize_waitlist(w)})
    finally:
        db.close()


# --- 17. GET /floor/api/history -------------------------------------------------------------

HISTORY_DAYS_MAX = 30  # Gate 4 (ck): days=N clamp (contract section 12)


def _history_groups(db, loc: dict, d: date, table_names: dict[str, str],
                    name_map: dict[str, str]) -> dict:
    """One business date's history groups (the frozen single-day shapes)."""
    start, end = _utc_window_for_business_date(d)
    seatings = (
        db.query(FloorSeating)
        .filter(FloorSeating.location_guid == loc["guid"],
                FloorSeating.seated_at >= start,
                FloorSeating.seated_at < end)
        .order_by(FloorSeating.seated_at, FloorSeating.id)
        .all()
    )
    seatings_out = [
        {
            "seating_id": s.id,
            "table_guid": s.table_guid,
            "table_name": table_names.get(s.table_guid, s.table_guid[:8]),
            "party_size": s.party_size,
            "seated_at": _iso_z(s.seated_at),
            "cleared_at": _iso_z(s.cleared_at),
            "server_employee_guid": s.server_employee_guid_at_seat,
            "server_name": _name_or_guid(
                name_map, s.server_employee_guid_at_seat),
            "reservation_id": s.reservation_id,
            "waitlist_id": s.waitlist_id,
        }
        for s in seatings
    ]
    res_rows = (
        db.query(FloorReservation)
        .filter(FloorReservation.location_guid == loc["guid"],
                FloorReservation.reserved_for >= start,
                FloorReservation.reserved_for < end,
                FloorReservation.status.in_(
                    ["seated", "no_show", "cancelled"]))
        .order_by(FloorReservation.reserved_for, FloorReservation.id)
        .all()
    )
    wl_rows = (
        db.query(FloorWaitlistEntry)
        .filter(FloorWaitlistEntry.location_guid == loc["guid"],
                FloorWaitlistEntry.joined_at >= start,
                FloorWaitlistEntry.joined_at < end,
                FloorWaitlistEntry.status.in_(["seated", "left"]))
        .order_by(FloorWaitlistEntry.joined_at, FloorWaitlistEntry.id)
        .all()
    )
    return {
        "seatings": seatings_out,
        "reservations": [
            _serialize_reservation(r, with_notes=False) for r in res_rows
        ],
        "waitlist": [_serialize_waitlist(w) for w in wl_rows],
    }


@floor_bp.route("/api/history", methods=["GET"])
def api_history():
    loc, err = _loc_or_400()
    if err:
        return err
    d, err = _date_param_or_today()
    if err:
        return err
    # Gate 4 (ck): backfill view - days=N (default 1, max 30). Absent or
    # days<=1 keeps the frozen single-day shape; days>1 returns per-day
    # buckets {ok, days:[{date, seatings, reservations, waitlist}]} ending
    # at the anchor date, most recent day first.
    raw_days = request.args.get("days")
    days = 1
    if raw_days is not None:
        try:
            days = int(raw_days.strip())
        except (ValueError, AttributeError):
            return jsonify({"ok": False, "error": "bad_days"}), 400
        days = max(1, min(days, HISTORY_DAYS_MAX))
    db = _open_db()
    try:
        # Gate 4 (ck): lazy no-show flag runs on every history read.
        _apply_noshow_flags(db, loc)
        # Table names: include soft-deleted rows - historical seatings join
        # on old GUIDs (contract section 3).
        table_names = {
            t.guid: t.name
            for t in db.query(ToastTableCfg)
            .filter(ToastTableCfg.location_guid == loc["guid"]).all()
        }
        name_map = _employee_name_map(loc)
        if days > 1:
            buckets = []
            for off in range(days):
                day = d - timedelta(days=off)
                groups = _history_groups(db, loc, day, table_names, name_map)
                buckets.append({"date": day.isoformat(), **groups})
            return jsonify({"ok": True, "days": buckets})
        groups = _history_groups(db, loc, d, table_names, name_map)
        return jsonify({"ok": True, "date": d.isoformat(), **groups})
    finally:
        db.close()


# --- 18. POST /floor/api/sync (manager) -------------------------------------------------------

@floor_bp.route("/api/sync", methods=["POST"])
def api_sync():
    deny = _manager_or_403()
    if deny:
        return deny
    loc, err = _loc_or_400()
    if err:
        return err
    # LAZY import (contract section 10): SA-1 builds toast_config_sync in
    # parallel - this module must import clean without it.
    try:
        sync_mod = importlib.import_module("app.services.toast_config_sync")
    except Exception:
        log.warning("floor: toast_config_sync unavailable")
        return jsonify({"ok": False, "error": "sync_unavailable"}), 503
    try:
        counts = sync_mod.sync_location(loc["key"])
    except Exception:
        log.exception("floor: sync_location failed for %s", loc["key"])
        return jsonify({"ok": False, "error": "sync_failed"}), 502
    if not isinstance(counts, dict):
        counts = {}
    return jsonify({"ok": True, **counts})
