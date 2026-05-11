"""Team admin page — partner-only.

GET  /partner/team             — list active + inactive users
POST /partner/team/add         — create user (full_name, permission_level, email/phone optional)
POST /partner/team/<id>/reset  — reset passcode (admin types a temp one; user forced to change on login)
POST /partner/team/<id>/toggle — toggle active flag
POST /partner/team/<id>/edit   — edit fields (name / email / phone / permission_level / store_scope)

All endpoints are guarded by require_level('partner') so only Sam + Masood
(once Sam adds him) can touch the roster. Passcode uniqueness is enforced
across active users — duplicate set/reset attempts are rejected.
"""
from __future__ import annotations

import re

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import SessionLocal
from app.models import User
from app.web.permissions import LEVELS, require_level

team_bp = Blueprint("team", __name__)

PASSCODE_RE = re.compile(r"^[\d*#@+%\-$]{5}$")

# UI dropdown collapses level + store into one. Format: "<level>|<store>".
# Sam (2026-05-11): the GM/Manager/Expo options should say "GM Copperfield"
# etc. so it's obvious the access is location-specific.
ROLE_OPTIONS = [
    ("partner|",              "Partner",              "partner",          None),
    ("corporate|",            "Corporate",            "corporate",        None),
    ("gm|tomball",            "GM — Tomball",         "gm",               "tomball"),
    ("gm|copperfield",        "GM — Copperfield",     "gm",               "copperfield"),
    ("manager|tomball",       "Manager — Tomball",    "manager",          "tomball"),
    ("manager|copperfield",   "Manager — Copperfield","manager",          "copperfield"),
    ("expo|tomball",          "Expo — Tomball",       "expo",             "tomball"),
    ("expo|copperfield",      "Expo — Copperfield",   "expo",             "copperfield"),
    ("corporate-driver|",     "Corporate Driver",     "corporate-driver", None),
]
_OPTION_TO_LEVEL_SCOPE = {opt: (lvl, sc) for opt, _label, lvl, sc in ROLE_OPTIONS}


def _parse_role(role_value: str) -> tuple[str | None, str | None]:
    """Return (level, store_scope) from a 'level|scope' dropdown value, or
    (None, None) if invalid. Plain 'partner' / 'corporate' use empty scope."""
    return _OPTION_TO_LEVEL_SCOPE.get((role_value or "").strip(), (None, None))


def _role_value_for(u) -> str:
    """Build the 'level|scope' string a user's current row corresponds to."""
    return f"{u.permission_level}|{u.store_scope or ''}"


def _norm_phone(s: str | None) -> str | None:
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"


def _passcode_taken(db, passcode: str, excluding_user_id: int | None = None) -> bool:
    q = db.query(User).filter(User.active.is_(True))
    if excluding_user_id is not None:
        q = q.filter(User.id != excluding_user_id)
    for u in q.all():
        if check_password_hash(u.passcode_hash, passcode):
            return True
    return False


@team_bp.route("/partner/team", methods=["GET"])
@require_level("partner")
def team_page():
    db = SessionLocal()
    try:
        users = (db.query(User)
                   .order_by(User.active.desc(), User.permission_level.asc(), User.full_name.asc())
                   .all())
        g.current_store = "partner"
        g.store_label = "Partner"
        g.current_location = "both"
        return render_template(
            "team.html",
            users=users,
            role_options=ROLE_OPTIONS,
            role_value_for=_role_value_for,
            success=request.args.get("success"),
            error=request.args.get("error"),
        )
    finally:
        db.close()


@team_bp.route("/partner/team/add", methods=["POST"])
@require_level("partner")
def team_add():
    full_name = (request.form.get("full_name") or "").strip()
    email = (request.form.get("email") or "").strip() or None
    phone = _norm_phone(request.form.get("phone"))
    role = (request.form.get("role") or "").strip()
    level, store_scope = _parse_role(role)
    passcode = (request.form.get("passcode") or "").strip()

    if not full_name:
        return redirect(url_for("team.team_page", error="Full name is required."))
    if level is None:
        return redirect(url_for("team.team_page", error=f"Invalid role: {role!r}"))
    if not PASSCODE_RE.match(passcode):
        return redirect(url_for("team.team_page",
                                error="Passcode must be exactly 5 characters (digits or * # @ + % - $)."))

    db = SessionLocal()
    try:
        if email:
            if db.query(User).filter(User.email == email).first():
                return redirect(url_for("team.team_page", error=f"Email {email} already in use."))
        if phone:
            if db.query(User).filter(User.phone == phone).first():
                return redirect(url_for("team.team_page", error=f"Phone {phone} already in use."))
        if _passcode_taken(db, passcode):
            return redirect(url_for("team.team_page",
                                    error="That passcode is taken — pick a different one."))

        u = User(
            full_name=full_name,
            email=email,
            phone=phone,
            passcode_hash=generate_password_hash(passcode),
            permission_level=level,
            store_scope=store_scope,
            first_login_done=False,
            active=True,
        )
        db.add(u)
        db.commit()
        return redirect(url_for("team.team_page",
                                success=f"Added {full_name} as {level}. Temp passcode {passcode} — they'll be forced to change on first login."))
    finally:
        db.close()


@team_bp.route("/partner/team/<int:user_id>/reset", methods=["POST"])
@require_level("partner")
def team_reset(user_id: int):
    passcode = (request.form.get("passcode") or "").strip()
    if not PASSCODE_RE.match(passcode):
        return redirect(url_for("team.team_page",
                                error="Passcode must be exactly 5 characters (digits or * # @ + % - $)."))
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return redirect(url_for("team.team_page", error="User not found."))
        if _passcode_taken(db, passcode, excluding_user_id=u.id):
            return redirect(url_for("team.team_page",
                                    error="That passcode is taken — pick a different one."))
        u.passcode_hash = generate_password_hash(passcode)
        u.first_login_done = False
        u.failed_attempts = 0
        u.lockout_until = None
        # Bump session_version so any active session for this user is
        # force-logged-out on its next request.
        u.session_version = (u.session_version or 1) + 1
        db.commit()
        return redirect(url_for("team.team_page",
                                success=f"Reset {u.full_name}'s passcode to {passcode}. They were logged out everywhere and will be forced to change it on next login."))
    finally:
        db.close()


@team_bp.route("/partner/team/<int:user_id>/toggle", methods=["POST"])
@require_level("partner")
def team_toggle(user_id: int):
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return redirect(url_for("team.team_page", error="User not found."))
        if u.id == (g.current_user.id if g.current_user else None):
            return redirect(url_for("team.team_page",
                                    error="You can't deactivate yourself."))
        u.active = not u.active
        # Bump version so an active session is invalidated immediately.
        u.session_version = (u.session_version or 1) + 1
        db.commit()
        state = "activated" if u.active else "deactivated"
        return redirect(url_for("team.team_page",
                                success=f"{u.full_name} {state}{' (logged out everywhere)' if not u.active else ''}."))
    finally:
        db.close()


@team_bp.route("/partner/team/<int:user_id>/edit", methods=["POST"])
@require_level("partner")
def team_edit(user_id: int):
    full_name = (request.form.get("full_name") or "").strip()
    email = (request.form.get("email") or "").strip() or None
    phone = _norm_phone(request.form.get("phone"))
    role = (request.form.get("role") or "").strip()
    level, store_scope = _parse_role(role)

    if not full_name:
        return redirect(url_for("team.team_page", error="Full name is required."))
    if level is None:
        return redirect(url_for("team.team_page", error=f"Invalid role: {role!r}"))

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return redirect(url_for("team.team_page", error="User not found."))
        if email and email != u.email:
            if db.query(User).filter(User.email == email, User.id != u.id).first():
                return redirect(url_for("team.team_page", error=f"Email {email} already in use."))
        if phone and phone != u.phone:
            if db.query(User).filter(User.phone == phone, User.id != u.id).first():
                return redirect(url_for("team.team_page", error=f"Phone {phone} already in use."))

        role_changed = (u.permission_level != level or (u.store_scope or "") != (store_scope or ""))
        u.full_name = full_name
        u.email = email
        u.phone = phone
        u.permission_level = level
        u.store_scope = store_scope
        # Force-logout the user if their role moved — their authority just
        # changed and their existing session may have stale partner_auth_ok
        # or store_scope state.
        if role_changed:
            u.session_version = (u.session_version or 1) + 1
        db.commit()
        suffix = " (logged out everywhere)" if role_changed else ""
        return redirect(url_for("team.team_page", success=f"Updated {full_name}.{suffix}"))
    finally:
        db.close()
