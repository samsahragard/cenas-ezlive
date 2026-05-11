"""Partner-only ezCater live-tracking admin page.

Lists today + upcoming orders, lets the manager paste the ezCater
customer-tracker URL per order (https://delivery-tracking.ezcater.com/
delivery/<uuid>), then polls the public delivery_tracking API for the
driver's current GPS + status key. Phase 1 — manual paste; Phase 2 will
automate the UUID capture from email and/or portal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, g

from app.db import SessionLocal
from app.models import Order

log = logging.getLogger(__name__)

ezc_live = Blueprint("ezcater_live", __name__)


def _enforce_partner():
    if not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login"))
    return None


@ezc_live.route("/partner/developer/ezcater-tracking", methods=["GET"])
def page():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    # Today + next 14 days, only orders that aren't cancelled.
    today_iso = datetime.now().strftime("%Y-%m-%d")
    cutoff_iso = (datetime.now() + timedelta(days=14)).strftime("%Y-%m-%d")
    db = SessionLocal()
    try:
        orders = (db.query(Order)
                    .filter(Order.delivery_date >= today_iso)
                    .filter(Order.delivery_date <= cutoff_iso)
                    .filter(Order.status != "cancelled")
                    .order_by(Order.delivery_date.asc(), Order.deliver_at.asc())
                    .all())
    finally:
        db.close()
    # Partner context for sidebar
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    return render_template(
        "ezcater_live.html",
        active="dev_ezcater_live",
        page_title="Ezcater · Live Tracking",
        orders=orders,
        success=request.args.get("success"),
        error=request.args.get("error"),
    )


@ezc_live.route("/partner/developer/ezcater-tracking/<external_order_id>/save", methods=["POST"])
def save_tracking(external_order_id: str):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    from app.services.ezcater_live_tracker import extract_tracking_uuid, poll_one
    raw = (request.form.get("tracking_url") or "").strip()
    uuid_ = extract_tracking_uuid(raw)
    if not uuid_:
        return redirect(url_for("ezcater_live.page",
                                error=f"Couldn't find a UUID in {raw[:80]!r}. Paste the full tracker URL."))
    db = SessionLocal()
    try:
        o = db.query(Order).filter(Order.external_order_id == external_order_id).first()
        if not o:
            return redirect(url_for("ezcater_live.page", error=f"Order {external_order_id} not found."))
        o.delivery_tracking_id = uuid_
        # Immediately poll so the row shows live data right away.
        poll_one(o)
        db.commit()
    finally:
        db.close()
    return redirect(url_for("ezcater_live.page",
                            success=f"Tracking UUID saved for {external_order_id}; first poll done."))


@ezc_live.route("/partner/developer/ezcater-tracking/<external_order_id>/poll", methods=["POST"])
def poll_one_route(external_order_id: str):
    gate = _enforce_partner()
    if gate is not None:
        return gate
    from app.services.ezcater_live_tracker import poll_one
    db = SessionLocal()
    try:
        o = db.query(Order).filter(Order.external_order_id == external_order_id).first()
        if not o:
            return jsonify({"ok": False, "error": "order not found"}), 404
        if not o.delivery_tracking_id:
            return jsonify({"ok": False, "error": "no tracking_id stored"}), 400
        poll_one(o)
        db.commit()
        return jsonify({
            "ok": True,
            "status_key": o.ezcater_status_key,
            "lat": o.ezcater_driver_lat,
            "lng": o.ezcater_driver_lng,
            "updated_at": o.ezcater_status_updated_at.isoformat() if o.ezcater_status_updated_at else None,
        })
    finally:
        db.close()


@ezc_live.route("/partner/developer/ezcater-tracking/poll-all", methods=["POST"])
def poll_all_route():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    from app.services.ezcater_live_tracker import poll_active
    result = poll_active(limit=25)
    return redirect(url_for("ezcater_live.page",
                            success=(f"Polled {result['polled']} orders — "
                                     f"{result['updated']} got fresh data, "
                                     f"{result['no_data']} returned no data.")))


@ezc_live.route("/partner/developer/ezcater-tracking/sync", methods=["POST"])
def sync_from_ezmanage():
    """Bulk-import tracking URLs from ezManage. Posted by the bookmarklet
    Sam runs while logged into ezmanage.ezcater.com. JSON body shape:
        {"updates": [{"order_number": "T4Y-F7J", "tracking_url": "https://..."}]}
    For each row: find Order by external_order_id (with-dash form), extract
    UUID from the URL, save, immediately poll. Returns per-row outcome.
    """
    gate = _enforce_partner()
    if gate is not None:
        return gate
    from app.services.ezcater_live_tracker import extract_tracking_uuid, poll_one
    payload = request.get_json(silent=True) or {}
    updates = payload.get("updates") or []
    results = {"saved": 0, "polled": 0, "skipped": [], "not_found": []}
    db = SessionLocal()
    try:
        for u in updates:
            on_raw = (u.get("order_number") or "").strip().upper()
            url = (u.get("tracking_url") or "").strip()
            uuid_ = extract_tracking_uuid(url)
            if not on_raw or not uuid_:
                results["skipped"].append({"order_number": on_raw, "reason": "missing data"})
                continue
            # Try with-dash form first (our DB), fall back to no-dash.
            with_dash = on_raw if "-" in on_raw else (f"{on_raw[:3]}-{on_raw[3:]}" if len(on_raw) >= 4 else on_raw)
            o = (db.query(Order).filter(Order.external_order_id == with_dash).first()
                 or db.query(Order).filter(Order.external_order_id == on_raw).first())
            if not o:
                results["not_found"].append(on_raw)
                continue
            o.delivery_tracking_id = uuid_
            body = poll_one(o)
            results["saved"] += 1
            if body:
                results["polled"] += 1
        db.commit()
    finally:
        db.close()
    return jsonify(results)
