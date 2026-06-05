"""Owner-only, READ-ONLY "View as user" QA surface (Sam-directed).

Lets a partner (owner) browse the app EXACTLY as another management/role
``User`` sees it -- their dashboard, their data, their permission gates -- to
verify that features render correctly and accurately from that seat. This is a
QA/verification tool, deliberately separate from the redacted Corporate Profile
Lab (which summarises profile state *without* impersonating anyone).

SAFETY (by construction):
  * OWNER-ONLY: start/stop/picker gate on ``g.real_user`` (the actually
    logged-in User), NEVER the effective/impersonated user, and require
    ``partner`` level. A non-partner can never start it, and even a stale
    ``view_as_user_id`` in the session is ignored unless the real user is a
    partner (the switch in permissions.load_current_user re-checks).
  * READ-ONLY while active: a before_request blocks non-idempotent methods
    (POST/PUT/PATCH/DELETE) so viewing can never mutate the target's data.
    GET/HEAD/OPTIONS and the ``/view-as`` control routes are exempt, so the
    owner can ALWAYS navigate and exit.
  * ALWAYS-VISIBLE banner + one-click exit (context processor ->
    base_dashboard) so the owner can never forget they are impersonating.
  * AUDITED: every start/stop writes a UserAuditLog row capturing both the
    real actor id and the effective (impersonated) id.
  * It never sets employee/driver session keys and never exposes
    passcodes/hashes/secrets -- it only flips which ``User`` g.current_user
    points at; the effective-user switch itself lives in
    ``app/web/permissions.load_current_user``.

v1 covers User-backed roles (partner / corporate / corporate_chef / gm / km /
assistant_km / prep_manager / foh_manager / expo / driver). Employee-self
(session["employee_id"]) and the driver app are separate auth systems -> a
deliberate phase-2.
"""
from __future__ import annotations

from flask import (
    Blueprint, current_app, g, has_request_context, redirect, render_template,
    request, session, url_for,
)
from markupsafe import escape
from sqlalchemy import event
from sqlalchemy.orm import Session as _SqlaSession

from app.db import SessionLocal
from app.models import User, UserAuditLog
from app.web.permissions import level_at_least, load_current_user


view_as_bp = Blueprint("view_as", __name__)

VIEW_AS_SESSION_KEY = "view_as_user_id"
# We deliberately DO NOT set this key. permissions.load_current_user already
# swaps g.current_user -> target (partner-gated, single chokepoint), and the
# tag-permission engine (_user_has) evaluates g.current_user, so impersonation
# is driven entirely by that one gated path. Setting impersonating_user_id would
# wire up a SECOND, ungated resolver in app/services/permissions._user_has
# (a privilege-escalation risk if the key ever survived into a non-partner
# session via a cross-principal login-fold). We keep the constant ONLY to
# DEFENSIVELY clear any pre-existing/stray value on stop / stale / target-gone.
IMPERSONATE_SESSION_KEY = "impersonating_user_id"


class ViewAsReadOnly(Exception):
    """Raised by the data-layer guard when a DB write is attempted during a
    read-only view-as session. Mapped to a 403 by an errorhandler in install()."""


_READONLY_LISTENER_INSTALLED = False


def _block_db_writes_during_view_as(session_, flush_context, instances):
    """SQLAlchemy before_flush hook: refuse ANY INSERT/UPDATE/DELETE while a
    partner is impersonating (g.viewing_as). This is the robust, complete
    read-only backstop -- it catches every DB-mutating handler (including the
    many side-effecting GET routes: lazy backfills, audit/access-log inserts)
    in ONE place instead of relying on a per-endpoint denylist that must
    enumerate them. A pure no-op for all normal (non-view-as) traffic. The
    feature's own view-as audit writes set g._view_as_audit_write to exempt
    themselves."""
    try:
        if not has_request_context():
            return
        if not getattr(g, "viewing_as", False):
            return
        if getattr(g, "_view_as_audit_write", False):
            return
    except Exception:
        return
    if session_.new or session_.deleted or any(
        session_.is_modified(o, include_collections=False) for o in session_.dirty
    ):
        raise ViewAsReadOnly(
            "Read-only while viewing as another user; database writes are blocked."
        )


def _real_user():
    """The actually-logged-in User (never the impersonated target).

    ``g.real_user`` is set by permissions.load_current_user (run from the
    _attach_current_user before_request). The fallback re-runs the loader
    defensively in case this is called outside that path.
    """
    ru = getattr(g, "real_user", None)
    if ru is None and not getattr(g, "_view_as_loaded", False):
        load_current_user()
        ru = getattr(g, "real_user", None)
    return ru


def _require_owner():
    """Return a Flask response if the REAL session user is not a partner;
    otherwise None (proceed). Gates the control routes on the real user, so
    impersonation can never be used to reach the control surface itself."""
    ru = _real_user()
    if ru is None:
        return redirect(url_for("keypad_auth.login", next=(request.full_path or "/view-as")))
    if not level_at_least(ru.permission_level, "partner"):
        return ("Forbidden -- the View-as QA tool is owner-only.", 403)
    return None


def _write_audit(db, action: str, *, target, ru, details: str) -> None:
    try:
        db.add(UserAuditLog(
            target_user_id=(target.id if target else None),
            target_label=((f"view_as:{target.full_name}")[:120] if target else "view_as:stop"),
            actor_user_id=(ru.id if ru else None),
            actor_label=(ru.full_name if ru else None),
            action=action,
            before_value=None,
            after_value=None,
            details=details[:500],
            ip=(request.remote_addr or None) if request else None,
        ))
        # Exempt our own audit write from the data-layer read-only guard
        # (needed when auditing during an active view-as session, e.g. the
        # A->B switch where g.viewing_as is already True).
        g._view_as_audit_write = True
        try:
            db.commit()
        finally:
            try:
                g._view_as_audit_write = False
            except Exception:
                pass
    except Exception:
        db.rollback()
        current_app.logger.exception("view_as audit write failed")


@view_as_bp.route("/view-as", methods=["GET"])
def view_as_picker():
    gate = _require_owner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        users = (
            db.query(User)
              .filter(User.active.is_(True))
              .order_by(User.permission_level.asc(), User.full_name.asc())
              .all()
        )
        rows = [
            {"id": u.id, "name": u.full_name, "role": u.permission_level,
             "store": (u.store_scope or "all stores")}
            for u in users
        ]
    finally:
        db.close()
    return render_template(
        "view_as_picker.html",
        users=rows,
        current_view_as=session.get(VIEW_AS_SESSION_KEY),
    )


@view_as_bp.route("/view-as/<int:user_id>", methods=["POST"])
def view_as_start(user_id: int):
    gate = _require_owner()
    if gate is not None:
        return gate
    ru = _real_user()
    if ru is not None and user_id == ru.id:
        # Viewing as yourself is a no-op.
        return redirect(url_for("view_as.view_as_picker"))
    db = SessionLocal()
    try:
        target = db.query(User).filter(User.id == user_id).first()
        if target is None or not target.active:
            return ("That user was not found or is inactive.", 404)
        # Bracket a direct A->B switch with a stop event for the prior target
        # so the audit trail has an explicit close (no two bare starts).
        prev = session.get(VIEW_AS_SESSION_KEY)
        if prev is not None and prev != user_id:
            _write_audit(db, "view_as_stop", target=None, ru=ru,
                         details=f"auto-stop view_as={prev} on switch to {user_id}")
        session[VIEW_AS_SESSION_KEY] = user_id
        # NB: impersonating_user_id is intentionally NOT set (see constant note).
        session.permanent = True
        _write_audit(
            db, "view_as_start", target=target, ru=ru,
            details=f"real={ru.id if ru else '?'} -> view_as={user_id} ({target.permission_level})",
        )
    finally:
        db.close()
    return redirect("/")


@view_as_bp.route("/view-as/stop", methods=["GET", "POST"])
def view_as_stop():
    # Always permitted so the owner can ALWAYS exit, even mid-impersonation
    # and even though the read-only guard is active.
    had = session.pop(VIEW_AS_SESSION_KEY, None)
    session.pop(IMPERSONATE_SESSION_KEY, None)
    if had is not None:
        ru = _real_user()
        db = SessionLocal()
        try:
            _write_audit(db, "view_as_stop", target=None, ru=ru, details=f"stopped view_as={had}")
        finally:
            db.close()
    return redirect("/")


def _view_as_banner_html() -> str:
    """Sticky red banner shown on every page while impersonating. Returns ''
    when not viewing-as. Names are escaped (defense against stored markup)."""
    if not getattr(g, "viewing_as", False):
        return ""
    target = getattr(g, "current_user", None)
    name = escape(getattr(target, "full_name", "user"))
    role = escape(getattr(target, "permission_level", "?"))
    try:
        stop = url_for("view_as.view_as_stop")
    except Exception:
        stop = "/view-as/stop"
    return (
        '<div style="position:sticky;top:0;z-index:99999;background:#b00020;color:#fff;'
        'padding:6px 14px;font:600 14px/1.4 system-ui,Segoe UI,sans-serif;display:flex;'
        'gap:12px;justify-content:space-between;align-items:center;">'
        f'<span>&#128065; VIEW-AS (read-only) &mdash; seeing the app as <b>{name}</b> ({role})</span>'
        f'<a href="{stop}" style="color:#fff;text-decoration:underline;font-weight:700;white-space:nowrap;">Exit view-as</a>'
        '</div>'
    )


def install(app) -> None:
    """Register the blueprint, the read-only guard, and the banner injector.

    MUST be called AFTER ezkeypad.install so the _attach_current_user
    before_request (which runs permissions.load_current_user and therefore
    sets g.viewing_as / g.real_user) is registered first -- Flask runs
    before_request hooks in registration order, so this guard sees the flag.
    """
    app.register_blueprint(view_as_bp)

    # Data-layer READ-ONLY backstop: block ALL DB writes while viewing-as. This
    # is the complete, future-proof guarantee -- it catches every DB-mutating
    # handler (incl. side-effecting GET routes: lazy backfills, audit/access-log
    # inserts) in ONE place, so the read-only promise does not depend on
    # enumerating endpoints. Installed once per process on the SQLAlchemy
    # Session class; a pure no-op outside an active view-as session.
    global _READONLY_LISTENER_INSTALLED
    if not _READONLY_LISTENER_INSTALLED:
        event.listen(_SqlaSession, "before_flush", _block_db_writes_during_view_as)
        _READONLY_LISTENER_INSTALLED = True

    @app.errorhandler(ViewAsReadOnly)
    def _view_as_write_blocked(_e):
        return (
            "Read-only while viewing as another user. Exit view-as (top banner) "
            "to make changes.",
            403,
        )

    # A SMALL denylist for GETs whose side effects fire BEFORE any DB commit, so
    # the flush guard above cannot catch them: produce confirm/cancel write JSON
    # state files + send vendor/manager emails; produce ingest_state writes state
    # files; ez_market makes up to 16 external Google Routes API calls. DB-only
    # mutating GETs (profile-lab/legal audit inserts, briefs feedback, lazy
    # backfills, etc.) are intentionally NOT listed -- the flush guard handles
    # them generically (and lets read-only pages still render where the handler
    # tolerates a blocked write).
    # NOTE: GETs that only perform external READ calls for rendering (Toast/Sling
    # dashboard + report feeds, the Google feasibility check) are deliberately
    # NOT denylisted -- they produce the data the owner needs to SEE in QA and
    # mutate nothing (idempotent reads + a benign cache file). "Read-only" here
    # means no DATA mutation and no real-world ACTION (orders/emails), enforced
    # by the data-layer guard + this action denylist -- not "zero external reads".
    mutating_get_endpoints = frozenset({
        "produce_order.confirm",
        "produce_order.cancel",
        "produce_order.ingest_state",
        "driver_system.ez_market",
    })

    @app.before_request
    def _view_as_readonly_guard():
        if not getattr(g, "viewing_as", False):
            return None
        # The owner must ALWAYS be able to use the view-as controls + exit.
        if (request.path or "").startswith("/view-as"):
            return None
        blocked = (
            "Read-only while viewing as another user. Exit view-as (top banner) "
            "to make changes.",
            403,
        )
        # Non-idempotent methods are blocked outright.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            return blocked
        # GETs to known state-mutating endpoints are also blocked (method is not
        # a safety proxy: some GET routes place orders / send email / write rows).
        if request.endpoint in mutating_get_endpoints:
            return blocked
        return None

    @app.context_processor
    def _inject_view_as_banner():
        return {
            "view_as_banner": _view_as_banner_html(),
            "view_as_active": bool(getattr(g, "viewing_as", False)),
        }
