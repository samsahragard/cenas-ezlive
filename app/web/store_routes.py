"""Per-store URL prefix layer.

The site has 4 store contexts:
  /dos/        → Tomball  (single location)
  /uno/        → Copperfield  (single location)
  /corporate/  → both locations (general manager view)
  /partner/    → both locations (owners only — Sam + Masood; for now mirrors corporate)

Each route here is a thin wrapper that sets `g.current_store` and `g.current_location`
on the request, then delegates to the underlying view via an internal call.
The existing top-level routes (e.g. /reports/labor) continue to work for backwards
compat, but the new sidebar links exclusively use the /<store>/... URLs.

Flask's `url_value_preprocessor` pulls the slug off the URL into `g`; `url_defaults`
auto-injects it back when generating URLs inside a store context. This means
templates can write `url_for('store.reports_labor')` and Flask will produce
`/dos/reports/labor` while in the Tomball scope.
"""
from __future__ import annotations

from flask import Blueprint, g, abort, request, render_template, redirect, url_for, session, jsonify

from datetime import datetime, timedelta

from app.db import get_db
from app.models import Driver, DriverShift, DriverLocation
from app.web.driver_routes import issue_temp_password, LOCATION_LABELS

# slug → location filter for downstream report functions
STORE_TO_LOCATION = {
    "dos":       "tomball",
    "uno":       "copperfield",
    "corporate": "both",
    "partner":   "both",
}

STORE_LABELS = {
    "dos":       "Tomball",
    "uno":       "Copperfield",
    "corporate": "Corporate",
    "partner":   "Partner",
}

# Marquee branding shown on the store picker
STORE_BRAND = {
    "dos":       {"marquee": "DOS MAS", "tagline": "Tomball · 27727 Tomball Pkwy"},
    "uno":       {"marquee": "UNO MAS", "tagline": "Copperfield · 15650 FM 529"},
    "corporate": {"marquee": "CORPORATE", "tagline": "Both locations"},
    "partner":   {"marquee": "PARTNER", "tagline": "Owners only"},
}


store_bp = Blueprint("store", __name__, url_prefix="/<store_slug>")


@store_bp.url_value_preprocessor
def _pull_store(endpoint, values):
    """Strip the slug off the URL and put it on `g` for downstream code."""
    if values is None:
        return
    slug = values.pop("store_slug", None)
    if slug not in STORE_TO_LOCATION:
        abort(404)
    g.current_store = slug
    g.current_location = STORE_TO_LOCATION[slug]
    g.store_label = STORE_LABELS[slug]


@store_bp.url_defaults
def _inject_store(endpoint, values):
    """When generating URLs from inside a store context, auto-fill the slug."""
    if "store_slug" not in values and getattr(g, "current_store", None):
        values["store_slug"] = g.current_store


@store_bp.before_request
def _partner_gate():
    """Second-factor auth for /partner/* — only owners (Sam + Masood) can see
    full management labor + future financial/legal sections. Everyone else
    sees Tomball / Copperfield / Corporate, which redact management data."""
    if getattr(g, "current_store", None) == "partner" and not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login"))


# ============== HOME DASHBOARD ==============

@store_bp.route("/")
def home():
    """Per-store manager dashboard. Re-uses the existing /home view but
    filtered to the current store's location."""
    from app.web import ezcater_routes
    return ezcater_routes.home()


# ============== OPERATIONS — VENDORS ==============

@store_bp.route("/produce/")
@store_bp.route("/produce/<path:subpath>")
def produce(subpath: str = ""):
    """Forward to the existing /produce/ stack. Sub-routes (cart, items, etc.)
    are reachable via /<store>/produce/<rest>; we 302 to keep behavior simple."""
    target = "/produce/" + (subpath or "")
    if request.query_string:
        target += "?" + request.query_string.decode()
    return redirect(target)


@store_bp.route("/produce/orders")
def produce_orders():
    """Vendors → Produce → Orders (per Sam's mockup). Price history + biggest
    movers + winner table. Currently org-wide (vendor pricing isn't location-
    specific in our setup); the per-store URL prefix is preserved for symmetry
    with the other sidebar leaves."""
    from app.web.reports import produce_orders as view
    return view()


# ============== OPERATIONS — EZCATER ==============

@store_bp.route("/orders")
def orders_list():
    """Per-location order list — maps to /orders/tomball or /orders/copperfield."""
    if g.current_location == "both":
        # Corporate / Partner — show home dashboard since there's no combined orders list yet
        return redirect(url_for("store.home"))
    return redirect(f"/orders/{g.current_location}")


@store_bp.route("/orders/processor")
def orders_processor():
    """PDF processor — same as the legacy /orders endpoint."""
    return redirect("/orders" + (("?" + request.query_string.decode()) if request.query_string else ""))


@store_bp.route("/review")
def review_queue():
    return redirect("/review" + (("?" + request.query_string.decode()) if request.query_string else ""))


@store_bp.route("/driver-tracking")
def driver_tracking():
    """Renamed from Manager Dashboard."""
    return redirect("/manager" + (("?" + request.query_string.decode()) if request.query_string else ""))


@store_bp.route("/driver-portal")
def driver_portal():
    return redirect("/driver" + (("?" + request.query_string.decode()) if request.query_string else ""))


# ============== OPERATIONS — DRIVERS ADMIN ==============

@store_bp.route("/drivers", methods=["GET"])
def drivers_admin():
    """Per-store driver admin: list / reset PW / deactivate.

    Per-location stores see only their own drivers; corporate + partner see all.
    Anyone past the site `cenas` gate can reach /uno/, /dos/, /corporate/.
    Partner is additionally gated by the partner-auth before_request hook above.
    """
    db = next(get_db())
    try:
        q = db.query(Driver)
        if g.current_location != "both":
            q = q.filter(Driver.location == g.current_location)
        rows = q.order_by(Driver.location, Driver.name).all()
        # latest shift per driver — drives the click-Active-to-see-location link
        from sqlalchemy import func
        latest_shift_for = {}
        if rows:
            ids = [d.id for d in rows]
            for did, sid in (db.query(DriverShift.driver_id, func.max(DriverShift.id))
                              .filter(DriverShift.driver_id.in_(ids))
                              .group_by(DriverShift.driver_id)
                              .all()):
                latest_shift_for[did] = sid
        return render_template(
            "driver_admin.html",
            drivers=rows,
            latest_shift_for=latest_shift_for,
            store_label=g.store_label,
            current_location=g.current_location,
            location_labels=LOCATION_LABELS,
            temp_pw=request.args.get("temp_pw"),
            temp_for=request.args.get("temp_for"),
            error=request.args.get("error"),
            active="drivers_admin",
        )
    finally:
        db.close()


@store_bp.route("/drivers/<int:driver_id>/reset", methods=["POST"])
def drivers_reset(driver_id: int):
    db = next(get_db())
    try:
        row = db.get(Driver, driver_id)
        if not row or (g.current_location != "both" and row.location != g.current_location):
            return redirect(url_for("store.drivers_admin", error="Driver not found at this store."))
        temp = issue_temp_password(db, row)
        return redirect(url_for("store.drivers_admin", temp_pw=temp, temp_for=row.name))
    finally:
        db.close()


# ============== OPERATIONS — DRIVERS LIVE MAP ==============

# Locations of the two stores — used as the initial map centre when no
# drivers are streaming yet
_STORE_CENTRES = {
    "tomball":     (30.1118, -95.6230),   # 27727 Tomball Pkwy
    "copperfield": (29.8730, -95.6428),   # 15650 FM 529
    "both":        (30.0,    -95.6),      # midpoint-ish
}


@store_bp.route("/drivers-live", methods=["GET"])
def drivers_live():
    """Live map: a marker per driver currently on shift, auto-refreshing."""
    centre_lat, centre_lng = _STORE_CENTRES.get(g.current_location, _STORE_CENTRES["both"])
    return render_template(
        "drivers_live.html",
        store_label=g.store_label,
        current_location=g.current_location,
        centre_lat=centre_lat,
        centre_lng=centre_lng,
        active="drivers_live",
    )


@store_bp.route("/drivers-live/positions.json", methods=["GET"])
def drivers_live_positions():
    """JSON feed for the map. Returns one record per driver currently on shift,
    with their most recent position fix. Filters to drivers at this store
    (or all-locations for corporate/partner)."""
    db = next(get_db())
    try:
        # Open shifts joined to driver, optionally filtered by location
        q = (db.query(DriverShift, Driver)
             .join(Driver, DriverShift.driver_id == Driver.id)
             .filter(DriverShift.ended_at.is_(None)))
        if g.current_location != "both":
            q = q.filter(Driver.location == g.current_location)
        results = []
        now = datetime.utcnow()
        for shift, drv in q.all():
            latest = (db.query(DriverLocation)
                      .filter(DriverLocation.shift_id == shift.id)
                      .order_by(DriverLocation.captured_at.desc())
                      .first())
            if not latest:
                continue
            seconds_ago = max(0, int((now - latest.captured_at).total_seconds()))
            results.append({
                "driver_id":      drv.id,
                "name":           drv.name,
                "location":       drv.location,
                "shift_started":  shift.started_at.isoformat() + "Z",
                "lat":            latest.lat,
                "lng":            latest.lng,
                "accuracy_m":     latest.accuracy_m,
                "speed_mps":      latest.speed_mps,
                "heading_deg":    latest.heading_deg,
                "captured_at":    latest.captured_at.isoformat() + "Z",
                "seconds_ago":    seconds_ago,
                "stale":          seconds_ago > 60,
            })
        return jsonify({"drivers": results, "now": now.isoformat() + "Z"})
    finally:
        db.close()


# ============== OPERATIONS — DRIVER SHIFT HISTORY / PLAYBACK ==============

import math


def _haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dlng/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def _shift_summary(db, shift):
    """Lightweight summary for one shift: duration, point count, distance, max speed.
    Distance is the sum of consecutive haversine hops, so it includes minor GPS
    jitter — fine for a glance-able number; not survey-grade."""
    pts = (db.query(DriverLocation)
           .filter(DriverLocation.shift_id == shift.id)
           .order_by(DriverLocation.captured_at.asc())
           .all())
    distance_m = 0.0
    max_speed = 0.0
    for prev, cur in zip(pts, pts[1:]):
        distance_m += _haversine_m(prev.lat, prev.lng, cur.lat, cur.lng)
        if cur.speed_mps and cur.speed_mps > max_speed:
            max_speed = cur.speed_mps
    end = shift.ended_at or datetime.utcnow()
    duration_s = max(0, int((end - shift.started_at).total_seconds()))
    return {
        "point_count": len(pts),
        "distance_m":  round(distance_m, 1),
        "duration_s":  duration_s,
        "max_speed_mps": round(max_speed, 2),
        "last_lat":    pts[-1].lat if pts else None,
        "last_lng":    pts[-1].lng if pts else None,
    }


def _scoped_driver(db, driver_id):
    """Look up a driver, enforcing the store's location scope."""
    drv = db.get(Driver, driver_id)
    if not drv:
        return None
    if g.current_location != "both" and drv.location != g.current_location:
        return None
    return drv


@store_bp.route("/drivers/<int:driver_id>/shifts", methods=["GET"])
def driver_shifts(driver_id: int):
    db = next(get_db())
    try:
        drv = _scoped_driver(db, driver_id)
        if not drv:
            return redirect(url_for("store.drivers_admin", error="Driver not found at this store."))
        shifts = (db.query(DriverShift)
                  .filter(DriverShift.driver_id == driver_id)
                  .order_by(DriverShift.started_at.desc())
                  .limit(60)
                  .all())
        rows = [{"shift": s, "summary": _shift_summary(db, s)} for s in shifts]
        return render_template(
            "driver_shifts.html",
            driver=drv,
            store_label=g.store_label,
            current_location=g.current_location,
            location_labels=LOCATION_LABELS,
            rows=rows,
            active="drivers_admin",
        )
    finally:
        db.close()


@store_bp.route("/drivers/<int:driver_id>/shifts/<int:shift_id>", methods=["GET"])
def driver_shift_playback(driver_id: int, shift_id: int):
    db = next(get_db())
    try:
        drv = _scoped_driver(db, driver_id)
        if not drv:
            return redirect(url_for("store.drivers_admin", error="Driver not found at this store."))
        shift = db.get(DriverShift, shift_id)
        if not shift or shift.driver_id != driver_id:
            return redirect(url_for("store.driver_shifts", driver_id=driver_id))
        summary = _shift_summary(db, shift)
        return render_template(
            "driver_playback.html",
            driver=drv,
            shift=shift,
            summary=summary,
            store_label=g.store_label,
            location_labels=LOCATION_LABELS,
            active="drivers_admin",
        )
    finally:
        db.close()


@store_bp.route("/drivers/<int:driver_id>/shifts/<int:shift_id>/track.json", methods=["GET"])
def driver_shift_track(driver_id: int, shift_id: int):
    db = next(get_db())
    try:
        drv = _scoped_driver(db, driver_id)
        if not drv:
            return jsonify({"error": "not found"}), 404
        shift = db.get(DriverShift, shift_id)
        if not shift or shift.driver_id != driver_id:
            return jsonify({"error": "not found"}), 404
        pts = (db.query(DriverLocation)
               .filter(DriverLocation.shift_id == shift_id)
               .order_by(DriverLocation.captured_at.asc())
               .all())
        return jsonify({
            "shift_id": shift_id,
            "started_at": shift.started_at.isoformat() + "Z",
            "ended_at":   shift.ended_at.isoformat() + "Z" if shift.ended_at else None,
            "points": [
                {
                    "lat": p.lat, "lng": p.lng,
                    "captured_at": p.captured_at.isoformat() + "Z",
                    "accuracy_m": p.accuracy_m,
                    "speed_mps": p.speed_mps,
                }
                for p in pts
            ],
        })
    finally:
        db.close()


@store_bp.route("/drivers/<int:driver_id>/toggle-active", methods=["POST"])
def drivers_toggle_active(driver_id: int):
    db = next(get_db())
    try:
        row = db.get(Driver, driver_id)
        if not row or (g.current_location != "both" and row.location != g.current_location):
            return redirect(url_for("store.drivers_admin", error="Driver not found at this store."))
        row.active = not row.active
        db.commit()
        return redirect(url_for("store.drivers_admin"))
    finally:
        db.close()


# ============== OPERATIONS — SCHEDULE ==============

@store_bp.route("/schedule")
@store_bp.route("/schedule/<view>")
def schedule(view: str = "weekly"):
    """Schedule (Sling). Children: BOH Roster / FOH Roster / All Roster / Weekly.
    Phase 1: 'weekly' shows the current schedule report; the BOH/FOH/All roster
    children are wired in Phase 2 with role classification."""
    from app.web.reports import schedule as schedule_view
    g.location_override = g.current_location if g.current_location != "both" else None
    return schedule_view()


@store_bp.route("/roster")
@store_bp.route("/roster/<view>")
def roster(view: str = "all"):
    """Roster — BOH / FOH / All. Phase 1 wires through a `roster_filter` query
    param the existing /reports/roster route doesn't yet honor; harmless for now."""
    from app.web.reports import roster as roster_view
    g.location_override = g.current_location if g.current_location != "both" else None
    g.roster_filter = view  # 'boh' / 'foh' / 'all' — Phase 2 will use this
    return roster_view()


# ============== INSIGHTS — PERFORMANCE / SALES / LABOR ==============

@store_bp.route("/reports/server-performance")
@store_bp.route("/reports/server-performance/<role>")
def server_performance(role: str = "all"):
    """Performance. Children: Server / Bartenders / Prep / All. Phase 1 renders
    the existing report; Phase 2 will filter by role."""
    from app.web.reports import server_performance as view
    g.location_override = g.current_location if g.current_location != "both" else None
    g.role_filter = role
    return view()


@store_bp.route("/reports/labor")
@store_bp.route("/reports/labor/<which>")
def labor(which: str = "all"):
    """Labor. Children: BOH / FOH / All."""
    from app.web.reports import labor as view
    g.location_override = g.current_location if g.current_location != "both" else None
    g.labor_filter = which
    return view()


@store_bp.route("/reports/sales")
@store_bp.route("/reports/sales/<channel>")
def sales(channel: str = "all"):
    """Sales. Children: Toast (in-store) / Online Toast / Ezcater / DoorDash / Uber / Total.
    Phase 1 shows the third-party report; Phase 2 wires per-channel."""
    from app.web.reports import third_party_sales as view
    g.location_override = g.current_location if g.current_location != "both" else None
    g.sales_channel = channel
    return view()
