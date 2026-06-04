"""Partner-only ezCater live tracking watch page.

Read-only observer: accepts customer tracker URLs, polls ezCater's public
tracking refresh endpoint, and renders map/status/late-risk data without
touching Cenas catering, driver, order, payroll, fulfillment, or notification
flows.
"""
from __future__ import annotations

import os

from flask import Blueprint, g, jsonify, redirect, render_template, request, session, url_for

from app.services import ezcater_tracking_watch as watch

ezcater_tracking_watch_bp = Blueprint("ezcater_tracking_watch", __name__)


def _enforce_partner():
    if not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login"))
    return None


@ezcater_tracking_watch_bp.route("/partner/developer/ezcater-live-map", methods=["GET"])
def page():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    g.current_store = "partner"
    g.store_label = "Partner"
    g.current_location = "both"
    data = watch.list_watch()
    google_maps_key = (
        os.getenv("GOOGLE_MAPS_BROWSER_KEY")
        or os.getenv("GOOGLE_MAPS_PUBLIC_KEY")
        or os.getenv("GOOGLE_MAPS_API_KEY")
        or ""
    ).strip()
    return render_template(
        "ezcater_tracking_watch.html",
        active="dev_ezcater_live_map",
        page_title="ezCater Live Map",
        orders=data["orders"],
        app_orders=watch.list_app_orders(),
        events=data["events"],
        google_maps_key=google_maps_key,
    )


@ezcater_tracking_watch_bp.route("/partner/developer/ezcater-live-map/api/orders", methods=["GET"])
def api_orders():
    gate = _enforce_partner()
    if gate is not None:
        return jsonify({"ok": False, "error": "partner login required"}), 401
    data = watch.list_watch()
    return jsonify({**data, "app_orders": watch.list_app_orders()})


@ezcater_tracking_watch_bp.route("/partner/developer/ezcater-live-map/api/orders", methods=["POST"])
def api_save():
    gate = _enforce_partner()
    if gate is not None:
        return jsonify({"ok": False, "error": "partner login required"}), 401
    payload = request.get_json(silent=True) or request.form.to_dict()
    try:
        order = watch.save_tracker(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "order": order})


@ezcater_tracking_watch_bp.route("/partner/developer/ezcater-live-map/api/import-text", methods=["POST"])
def api_import_text():
    gate = _enforce_partner()
    if gate is not None:
        return jsonify({"ok": False, "error": "partner login required"}), 401
    payload = request.get_json(silent=True) or request.form.to_dict()
    result = watch.import_text(payload.get("text") or "", payload)
    return jsonify({"ok": True, **result})


@ezcater_tracking_watch_bp.route("/partner/developer/ezcater-live-map/api/poll", methods=["POST"])
def api_poll():
    gate = _enforce_partner()
    if gate is not None:
        return jsonify({"ok": False, "error": "partner login required"}), 401
    result = watch.poll_all()
    data = watch.list_watch()
    return jsonify({"ok": True, "poll": result, **data, "app_orders": watch.list_app_orders(live_poll=True)})


@ezcater_tracking_watch_bp.route("/partner/developer/ezcater-live-map/api/orders/<uuid_>", methods=["DELETE"])
def api_delete(uuid_: str):
    gate = _enforce_partner()
    if gate is not None:
        return jsonify({"ok": False, "error": "partner login required"}), 401
    return jsonify({"ok": watch.delete_tracker(uuid_)})
