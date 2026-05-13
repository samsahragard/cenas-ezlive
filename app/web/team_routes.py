"""Team admin page — partner-only.

GET  /partner/team             — list active + inactive users (sorted by store, role, name)
POST /partner/team/add         — create user; auto-generates a 5-digit temp PIN, returned in banner
POST /partner/team/<id>/reset  — reset passcode (admin types a temp one OR clicks "generate")
POST /partner/team/<id>/toggle — toggle active flag (archive-only — never deletes)
POST /partner/team/<id>/edit   — edit fields; role changes audit-logged with before/after

All endpoints are guarded by require_level('partner') so only Sam + Masood
(once Sam adds him) can touch the roster. Passcode uniqueness is enforced
across active users — duplicate set/reset attempts are rejected.

Every mutation writes a row to UserAuditLog (append-only, append-only at
the ORM layer via before_delete listener). Role transitions specifically
log the before/after permission_level + store_scope pair so the trail
reads true for promotion/demotion audits. Sam: 2026-05-13 (Phase 0 Block 4
follow-up Team UI commit).
"""
from __future__ import annotations

import random
import re

from flask import Blueprint, flash, g, redirect, render_template, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import SessionLocal
from app.models import User, AccessRequest, UserAuditLog
from app.web.permissions import LEVELS, STORE_SCOPED_LEVELS, require_level

team_bp = Blueprint("team", __name__)

PASSCODE_RE = re.compile(r"^[\d*#@+%\-$]{5}$")

# Canonical 10 roles from samai's permission_system spec (dfde3de). Values
# match ROLE_PERMISSIONS keys in app/services/permissions.py so the new
# decorator-based gates resolve correctly. Display labels are human-friendly.
# Legacy aliases ('manager' → gm, 'corporate-driver' → driver) are NOT in
# the dropdown — Sam never wants to create new legacy rows. They survive in
# LEVELS for require_level rank checks against any stale pre-spec rows.
LEVEL_OPTIONS = [
    ("partner",         "Partner"),
    ("corporate",       "Corporate"),
    ("corporate_chef",  "Corporate Chef"),
    ("gm",              "GM"),
    ("km",              "Kitchen Manager"),
    ("assistant_km",    "Assistant KM"),
    ("prep_manager",    "Prep Manager"),
    ("foh_manager",     "FOH Manager"),
    ("expo",            "Expo"),
    ("driver",          "Driver"),
]
# Stores the Team admin can assign for store-scoped levels. Multi-select
# via the form's 'stores' checkbox list; CSV-stored in User.store_scope.
STORE_OPTIONS = [
    ("tomball",     "Tomball"),
    ("copperfield", "Copperfield"),
]


def _generate_temp_pin() -> str:
    """Random 5-digit numeric — matches driver_admin Reset PIN auto-gen UX
    so the partner can read it aloud to the new team member. Digits only
    (no special chars in the auto-gen path so verbal hand-off is clean)."""
    return "".join(str(random.randint(0, 9)) for _ in range(5))


def _audit_log(
    db,
    *,
    action: str,
    target_user: User | None,
    before_value: str | None = None,
    after_value: str | None = None,
    details: str | None = None,
) -> None:
    """Append a UserAuditLog row attributing the action to g.current_user.
    Caller is responsible for db.commit() — we share the calling
    transaction so the audit row + the User mutation land atomically."""
    actor = getattr(g, "current_user", None)
    db.add(UserAuditLog(
        target_user_id=target_user.id if target_user else None,
        target_label=(target_user.full_name if target_user else None),
        actor_user_id=actor.id if actor else None,
        actor_label=(actor.full_name if actor else None),
        action=action,
        before_value=before_value,
        after_value=after_value,
        details=details,
        ip=(request.remote_addr or None) if request else None,
    ))


def _role_state(level: str | None, store_scope: str | None) -> str:
    """Canonical 'role|store_scope' string for the audit log before/after
    columns. None store_scope renders as empty string."""
    return f"{level or ''}|{store_scope or ''}"


def _parse_role_form(form) -> tuple[str | None, str | None]:
    """Pull (level, store_scope_csv) out of a Team form submission. The
    stores come from one or more 'stores' checkboxes; for store-scoped
    levels (everyone except partner + corporate) at least one is required.
    Non-store levels get None. If the submitted role isn't in
    LEVEL_OPTIONS (e.g. stale 'manager' rows) we return (None, None) so
    the caller surfaces a 'pick a role' error — forces migration off any
    legacy value via the edit UI."""
    level = (form.get("permission_level") or "").strip()
    if level not in [lvl for lvl, _ in LEVEL_OPTIONS]:
        return None, None
    if level in STORE_SCOPED_LEVELS:
        stores = [s for s in form.getlist("stores") if s in [k for k, _ in STORE_OPTIONS]]
        if not stores:
            return level, None  # signal: scoped level needs at least one store
        return level, ",".join(stores)
    return level, None


def _user_stores_set(u) -> set[str]:
    """Set of store keys (tomball/copperfield) currently on this user."""
    return {s.strip() for s in (u.store_scope or "").split(",") if s.strip()}


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
        # Sort: active first, then by store_scope (NULL=partner/corporate at
        # top), then by role rank from LEVELS, then by name. This matches
        # Sam's spec: "sorted by store then role". Done in Python so we can
        # use LEVELS index as the role sort key rather than alphabetical
        # ordering (alphabetical would put 'assistant_km' above 'gm').
        users_raw = db.query(User).all()
        level_idx = {name: i for i, name in enumerate(LEVELS)}
        def _sort_key(u):
            return (
                0 if u.active else 1,
                u.store_scope or "",  # NULL store_scope sorts first
                level_idx.get(u.permission_level, len(LEVELS)),
                (u.full_name or "").lower(),
            )
        users = sorted(users_raw, key=_sort_key)
        pending_requests = (db.query(AccessRequest)
                              .filter(AccessRequest.status == "pending")
                              .order_by(AccessRequest.created_at.desc())
                              .all())
        g.current_store = "partner"
        g.store_label = "Partner"
        g.current_location = "both"
        return render_template(
            "team.html",
            users=users,
            pending_requests=pending_requests,
            level_options=LEVEL_OPTIONS,
            store_options=STORE_OPTIONS,
            user_stores_set=_user_stores_set,
            store_scoped_levels=STORE_SCOPED_LEVELS,
            success=request.args.get("success"),
            error=request.args.get("error"),
            temp_pw=request.args.get("temp_pw"),
            temp_for=request.args.get("temp_for"),
        )
    finally:
        db.close()


@team_bp.route("/partner/team/add", methods=["POST"])
@require_level("partner")
def team_add():
    full_name = (request.form.get("full_name") or "").strip()
    email = (request.form.get("email") or "").strip() or None
    phone = _norm_phone(request.form.get("phone"))
    level, store_scope = _parse_role_form(request.form)

    if not full_name:
        return redirect(url_for("team.team_page", error="Full name is required."))
    if level is None:
        return redirect(url_for("team.team_page", error="Pick a role."))
    if level in STORE_SCOPED_LEVELS and not store_scope:
        return redirect(url_for("team.team_page",
                                error=f"{level.replace('_', ' ').title()} needs at least one assigned store."))
    if level not in STORE_SCOPED_LEVELS and store_scope:
        # Partner / corporate roles get NULL store_scope — they see every
        # store implicitly. Reject the form rather than silently dropping
        # the value so Sam knows the picker mismatched the role.
        return redirect(url_for("team.team_page",
                                error=f"{level.replace('_', ' ').title()} sees every store — leave the store boxes unchecked."))

    db = SessionLocal()
    try:
        if email:
            if db.query(User).filter(User.email == email).first():
                return redirect(url_for("team.team_page", error=f"Email {email} already in use."))
        if phone:
            if db.query(User).filter(User.phone == phone).first():
                return redirect(url_for("team.team_page", error=f"Phone {phone} already in use."))

        # Auto-generate a 5-digit numeric temp PIN (driver_admin Reset PIN
        # pattern). Retry on collision so Sam never sees a uniqueness
        # error during the read-aloud flow. With only ~15 active users and
        # 100,000 possible PINs the collision rate is vanishingly small but
        # we still defend against it.
        temp_pin = _generate_temp_pin()
        for _ in range(20):
            if not _passcode_taken(db, temp_pin):
                break
            temp_pin = _generate_temp_pin()
        else:
            return redirect(url_for("team.team_page",
                                    error="Couldn't generate a unique temp PIN after 20 tries — try again."))

        u = User(
            full_name=full_name,
            email=email,
            phone=phone,
            passcode_hash=generate_password_hash(temp_pin),
            permission_level=level,
            store_scope=store_scope,
            first_login_done=False,
            active=True,
        )
        db.add(u)
        db.flush()  # populate u.id for the audit row
        _audit_log(
            db,
            action="create",
            target_user=u,
            after_value=_role_state(level, store_scope),
            details=f"created: email={email or '-'}, phone={phone or '-'}",
        )
        db.commit()
        # Redirect with temp_pw + temp_for so team.html renders the
        # driver_admin-style read-aloud banner. Banner says "won't be
        # shown again" — true; the plaintext is only in the redirect URL.
        return redirect(url_for("team.team_page",
                                temp_pw=temp_pin,
                                temp_for=full_name))
    finally:
        db.close()


@team_bp.route("/partner/team/<int:user_id>/reset", methods=["POST"])
@require_level("partner")
def team_reset(user_id: int):
    # Two paths: (a) admin clicked "Generate" → form has no passcode →
    # we auto-gen a 5-digit numeric one (driver_admin pattern), (b) admin
    # typed a passcode into the field → we validate + use that. Either
    # way we land in the read-aloud banner.
    typed = (request.form.get("passcode") or "").strip()
    auto_gen = not typed
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return redirect(url_for("team.team_page", error="User not found."))

        if auto_gen:
            passcode = _generate_temp_pin()
            for _ in range(20):
                if not _passcode_taken(db, passcode, excluding_user_id=u.id):
                    break
                passcode = _generate_temp_pin()
            else:
                return redirect(url_for("team.team_page",
                                        error="Couldn't generate a unique temp PIN after 20 tries — try again."))
        else:
            if not PASSCODE_RE.match(typed):
                return redirect(url_for("team.team_page",
                                        error="Passcode must be exactly 5 characters (digits or * # @ + % - $)."))
            if _passcode_taken(db, typed, excluding_user_id=u.id):
                return redirect(url_for("team.team_page",
                                        error="That passcode is taken — pick a different one."))
            passcode = typed

        u.passcode_hash = generate_password_hash(passcode)
        u.first_login_done = False
        u.failed_attempts = 0
        u.lockout_until = None
        # Bump session_version so any active session for this user is
        # force-logged-out on its next request.
        u.session_version = (u.session_version or 1) + 1
        _audit_log(
            db,
            action="passcode_reset",
            target_user=u,
            details=f"method={'auto' if auto_gen else 'typed'}, sessions invalidated",
        )
        db.commit()
        return redirect(url_for("team.team_page",
                                temp_pw=passcode,
                                temp_for=u.full_name))
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
        # Archive-only: we flip active=False, we never DELETE the row.
        # Re-activation flips it back. Audit row captures both transitions
        # so a 'who deactivated this person + when' question has an answer.
        was_active = u.active
        u.active = not u.active
        # Bump version so an active session is invalidated immediately.
        u.session_version = (u.session_version or 1) + 1
        _audit_log(
            db,
            action="deactivate" if was_active else "reactivate",
            target_user=u,
            details=("sessions invalidated" if was_active else "re-enabled, no session changes carry over"),
        )
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
    level, store_scope = _parse_role_form(request.form)

    if not full_name:
        return redirect(url_for("team.team_page", error="Full name is required."))
    if level is None:
        return redirect(url_for("team.team_page", error="Pick a role."))
    if level in STORE_SCOPED_LEVELS and not store_scope:
        return redirect(url_for("team.team_page",
                                error=f"{level.replace('_', ' ').title()} needs at least one assigned store."))
    if level not in STORE_SCOPED_LEVELS and store_scope:
        return redirect(url_for("team.team_page",
                                error=f"{level.replace('_', ' ').title()} sees every store — leave the store boxes unchecked."))

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

        # Snapshot the before-state so the audit row is self-describing.
        before_state = _role_state(u.permission_level, u.store_scope)
        before_name, before_email, before_phone = u.full_name, u.email, u.phone

        role_changed = (u.permission_level != level or (u.store_scope or "") != (store_scope or ""))
        changed_fields: list[str] = []
        if u.full_name != full_name:
            changed_fields.append("name")
        if u.email != email:
            changed_fields.append("email")
        if u.phone != phone:
            changed_fields.append("phone")

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

        # Audit log: role transitions get their own action so the trail is
        # easy to filter on ("show me every promotion/demotion this
        # quarter"). Non-role edits log as a single 'edit' row with the
        # touched-fields list in details.
        if role_changed:
            after_state = _role_state(level, store_scope)
            _audit_log(
                db,
                action="role_change",
                target_user=u,
                before_value=before_state,
                after_value=after_state,
                details=(f"also touched: {', '.join(changed_fields)}" if changed_fields else None),
            )
        elif changed_fields:
            _audit_log(
                db,
                action="edit",
                target_user=u,
                details=f"fields: {', '.join(changed_fields)}",
            )
        # If neither role nor fields changed we skip the audit row — saving
        # an unchanged form shouldn't pollute the trail.

        db.commit()
        suffix = " (logged out everywhere)" if role_changed else ""
        return redirect(url_for("team.team_page", success=f"Updated {full_name}.{suffix}"))
    finally:
        db.close()
