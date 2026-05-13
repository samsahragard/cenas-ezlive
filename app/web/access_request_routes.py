"""Public 'Request Access' flow.

Anyone who lands on /keypad-login without an account can hit /request-access,
submit their info, and Sam (or Masood) approves them from /partner/team.
Approval auto-generates a 5-digit temp passcode + creates the User row;
the temp passcode is shown ONCE in the admin success banner so Sam can
relay it (text/call) to the requester.

This is public — exempt from the global auth gate (see auth.py
EXEMPT_PREFIXES). Anonymous submissions are rate-limited by IP via
simple in-process counter (good enough for our scale; no public spam
expected since the URL is mainly reached from inside the Capacitor
app's keypad-login screen).
"""
from __future__ import annotations

import logging
import secrets
import string
import time
from collections import defaultdict
from datetime import datetime

from flask import Blueprint, redirect, render_template, request, url_for

from app.db import SessionLocal
from app.models import AccessRequest

log = logging.getLogger(__name__)

access_req = Blueprint("access_request", __name__)

# IP → list of submission timestamps (last hour). Trimmed inline.
_RATE_BUCKET: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW_SEC = 3600
_RATE_MAX_PER_HOUR = 6


def _client_ip() -> str:
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "?").split(",")[0].strip()


def _check_rate(ip: str) -> bool:
    """True if this IP is within budget. Side-effect: trims + appends."""
    now = time.time()
    bucket = _RATE_BUCKET[ip]
    _RATE_BUCKET[ip] = [t for t in bucket if t > now - _RATE_WINDOW_SEC]
    if len(_RATE_BUCKET[ip]) >= _RATE_MAX_PER_HOUR:
        return False
    _RATE_BUCKET[ip].append(now)
    return True


_ROLE_OPTIONS = [
    ("driver",           "Driver"),
    ("corporate-driver", "Corporate Driver"),
    ("expo",             "Expo"),
    ("manager",          "Manager"),
    ("gm",               "GM (General Manager)"),
    ("corporate",        "Corporate"),
    ("partner",          "Partner / Owner"),
]

# Drivers don't go through the admin-approval AccessRequest flow — they
# self-sign-up at /driver/signup with email + 5-digit PIN. Picking "Driver"
# on this form short-circuits to that page (with name/email/phone prefilled).
_DRIVER_ROLE = "driver"


@access_req.route("/request-access", methods=["GET"])
def request_access_page():
    return render_template(
        "request_access.html",
        role_options=_ROLE_OPTIONS,
        success=request.args.get("success"),
        error=request.args.get("error"),
        submitted_name=request.args.get("submitted_name", ""),
    )


@access_req.route("/request-access", methods=["POST"])
def request_access_submit():
    ip = _client_ip()
    if not _check_rate(ip):
        return redirect(url_for(
            "access_request.request_access_page",
            error="Too many requests from this network. Please try again in an hour or contact Sam directly.",
        ))

    full_name = (request.form.get("full_name") or "").strip()[:150]
    email = (request.form.get("email") or "").strip()[:200] or None
    phone = (request.form.get("phone") or "").strip()[:50] or None
    requested_role = (request.form.get("requested_role") or "").strip() or None
    if requested_role not in [k for k, _ in _ROLE_OPTIONS]:
        requested_role = None
    reason = (request.form.get("reason") or "").strip()[:1000] or None

    if not full_name:
        return redirect(url_for(
            "access_request.request_access_page",
            error="Full name is required.",
        ))
    if not (email or phone):
        return redirect(url_for(
            "access_request.request_access_page",
            error="Provide at least one of email or phone so we can contact you.",
        ))

    # Drivers self-sign-up — no admin approval needed. Hand off to
    # /driver/signup with the info they already typed pre-filled so they
    # only have to add location + PIN.
    if requested_role == _DRIVER_ROLE:
        return redirect(url_for(
            "driver.driver_signup",
            name=full_name,
            email=email or "",
            phone=phone or "",
        ))

    db = SessionLocal()
    try:
        req = AccessRequest(
            full_name=full_name, email=email, phone=phone,
            requested_role=requested_role, reason=reason,
            status="pending",
        )
        db.add(req)
        db.commit()
        log.info("access_request: %s (%s / %s) requested %s",
                 full_name, email, phone, requested_role)
    finally:
        db.close()

    return redirect(url_for(
        "access_request.request_access_page",
        success="Request received — an admin will review shortly.",
        submitted_name=full_name,
    ))


# ---------------------------------------------------------------------
# Admin endpoints — partner-only. Wired in here rather than team_routes.py
# so the access-request model stays in one file.
# ---------------------------------------------------------------------

PASSCODE_ALPHABET = string.digits  # generated temp passcodes are 5 digits


def _generate_temp_passcode(db) -> str:
    """5-digit numeric, unique across active Users (matches the team_add
    PASSCODE_RE uniqueness contract)."""
    from app.models import User
    from werkzeug.security import check_password_hash
    for _ in range(50):
        candidate = "".join(secrets.choice(PASSCODE_ALPHABET) for _ in range(5))
        active_users = db.query(User).filter(User.active.is_(True),
                                              User.passcode_hash.is_not(None)).all()
        clash = any(check_password_hash(u.passcode_hash, candidate) for u in active_users)
        if not clash:
            return candidate
    # Astronomically unlikely; fall back to a 7-digit code if 50 tries collide
    return "".join(secrets.choice(PASSCODE_ALPHABET) for _ in range(7))


@access_req.route("/partner/team/request/<int:req_id>/approve", methods=["POST"])
def approve_request(req_id: int):
    from app.web.permissions import require_level
    from app.models import User
    from werkzeug.security import generate_password_hash
    from flask import g

    # Manually invoke require_level so we don't need a decorator import
    gate = require_level("partner")(lambda: None)()
    if gate is not None:
        return gate

    chosen_role = (request.form.get("permission_level") or "").strip()
    chosen_stores = ",".join(request.form.getlist("stores")) or None
    if chosen_role not in {"partner", "corporate", "gm", "manager", "expo", "corporate-driver"}:
        return redirect(url_for("team.team_page", error="Pick a role to approve with."))

    db = SessionLocal()
    try:
        req = db.query(AccessRequest).filter_by(id=req_id, status="pending").first()
        if not req:
            return redirect(url_for("team.team_page", error="Request not found or already reviewed."))
        # Conflicts
        if req.email and db.query(User).filter(User.email == req.email).first():
            return redirect(url_for("team.team_page",
                                    error=f"User with email {req.email} already exists. Decline this request and reset that user's passcode instead."))
        if req.phone and db.query(User).filter(User.phone == req.phone).first():
            return redirect(url_for("team.team_page",
                                    error=f"User with phone {req.phone} already exists. Decline this request and reset that user's passcode instead."))

        temp = _generate_temp_passcode(db)
        u = User(
            full_name=req.full_name,
            email=req.email,
            phone=req.phone,
            passcode_hash=generate_password_hash(temp),
            permission_level=chosen_role,
            store_scope=chosen_stores if chosen_role in {"gm", "manager", "expo"} else None,
            first_login_done=False,
            active=True,
        )
        db.add(u)
        db.flush()
        req.status = "approved"
        req.reviewed_at = datetime.utcnow()
        req.reviewed_by_user_id = (g.current_user.id if g.get("current_user") else None)
        req.created_user_id = u.id
        req.temp_passcode_one_shot = temp
        db.commit()
        return redirect(url_for(
            "team.team_page",
            success=f"Approved {req.full_name} as {chosen_role}. Temp passcode: {temp} — relay it (they'll be forced to change on first login).",
        ))
    finally:
        db.close()


@access_req.route("/partner/team/request/<int:req_id>/decline", methods=["POST"])
def decline_request(req_id: int):
    from app.web.permissions import require_level
    from flask import g
    gate = require_level("partner")(lambda: None)()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        req = db.query(AccessRequest).filter_by(id=req_id, status="pending").first()
        if not req:
            return redirect(url_for("team.team_page", error="Request not found or already reviewed."))
        req.status = "declined"
        req.reviewed_at = datetime.utcnow()
        req.reviewed_by_user_id = (g.current_user.id if g.get("current_user") else None)
        db.commit()
        return redirect(url_for(
            "team.team_page",
            success=f"Declined access request from {req.full_name}.",
        ))
    finally:
        db.close()
