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

PASSCODE_RE = re.compile(r"^\d{5}$")


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
            levels=LEVELS,
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
    level = (request.form.get("permission_level") or "manager").strip()
    store_scope = (request.form.get("store_scope") or "").strip() or None
    passcode = (request.form.get("passcode") or "").strip()

    if not full_name:
        return redirect(url_for("team.team_page", error="Full name is required."))
    if level not in LEVELS:
        return redirect(url_for("team.team_page", error=f"Invalid level: {level}"))
    if not PASSCODE_RE.match(passcode):
        return redirect(url_for("team.team_page", error="Passcode must be exactly 5 digits."))

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
        return redirect(url_for("team.team_page", error="Passcode must be exactly 5 digits."))
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
        db.commit()
        return redirect(url_for("team.team_page",
                                success=f"Reset {u.full_name}'s passcode to {passcode}. They'll be forced to change it on next login."))
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
        db.commit()
        state = "activated" if u.active else "deactivated"
        return redirect(url_for("team.team_page", success=f"{u.full_name} {state}."))
    finally:
        db.close()


@team_bp.route("/partner/team/<int:user_id>/edit", methods=["POST"])
@require_level("partner")
def team_edit(user_id: int):
    full_name = (request.form.get("full_name") or "").strip()
    email = (request.form.get("email") or "").strip() or None
    phone = _norm_phone(request.form.get("phone"))
    level = (request.form.get("permission_level") or "manager").strip()
    store_scope = (request.form.get("store_scope") or "").strip() or None

    if not full_name:
        return redirect(url_for("team.team_page", error="Full name is required."))
    if level not in LEVELS:
        return redirect(url_for("team.team_page", error=f"Invalid level: {level}"))

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

        u.full_name = full_name
        u.email = email
        u.phone = phone
        u.permission_level = level
        u.store_scope = store_scope
        db.commit()
        return redirect(url_for("team.team_page", success=f"Updated {full_name}."))
    finally:
        db.close()
