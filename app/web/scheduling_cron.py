"""Schedules V2 - Block 6: the system cron endpoint that sends due shift alarms.

POST /internal/scheduling/cron/process-shift-alarms  (empty body)

Token-gated via the CRON_TOKEN env var, read from (in order) an
`Authorization: Bearer <CRON_TOKEN>` header (canonical), an `X-Cron-Token`
header, or a `?token=` query arg - the same 3-way read driver_system's crons
use, so one trigger config works across both. Fail-closed: if CRON_TOKEN is
unset, or the presented token does not match, the endpoint 403s and does NO
work. The token value is never logged and never leaves the web service.

This path is added to auth.py EXEMPT_PREFIXES (the /internal/scheduling/cron/
prefix) so the global password gate does not 302-redirect it to /keypad-login
before this token check runs - the same fix the driver crons use for /cron/.
The body itself does the auth, so the exemption only bypasses the session gate,
never authorization.

aick owns the per-minute trigger + the CRON_TOKEN env at B6 merge-time (his
infra lane); this module just exposes the endpoint it calls.
"""
from __future__ import annotations

import os

from flask import Blueprint, jsonify, request

from app.services.scheduling_alarms import process_due_alarms
from app.services.scheduling_offers import expire_due  # B9: expire stale offers/swaps

scheduling_cron_bp = Blueprint("scheduling_cron", __name__)


def _presented_token() -> str | None:
    """The cron token from the request: Bearer header (canonical), X-Cron-Token,
    or ?token= - mirrors driver_system._extract_cron_token so one trigger works
    for both cron families."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return request.headers.get("X-Cron-Token") or request.args.get("token")


@scheduling_cron_bp.route(
    "/internal/scheduling/cron/process-shift-alarms", methods=["POST"]
)
def process_shift_alarms():
    """Send every shift alarm whose time has arrived. Token-gated; fail-closed."""
    expected = os.getenv("CRON_TOKEN")
    if not expected or _presented_token() != expected:
        # 403 with no detail - never confirm whether CRON_TOKEN is set.
        return jsonify({"ok": False, "error": "forbidden"}), 403
    summary = process_due_alarms()
    return jsonify({"ok": True, **summary}), 200


@scheduling_cron_bp.route(
    "/internal/scheduling/cron/expire-shift-offers", methods=["POST"]
)
def expire_shift_offers():
    """B9: expire stale shift offers + swaps past their expires_at. Token-gated
    (same CRON_TOKEN + fail-closed pattern as process-shift-alarms; covered by the
    same /internal/scheduling/cron/ EXEMPT prefix)."""
    expected = os.getenv("CRON_TOKEN")
    if not expected or _presented_token() != expected:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    summary = expire_due()
    return jsonify({"ok": True, **summary}), 200
