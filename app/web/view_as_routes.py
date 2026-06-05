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
from app.models import Driver, Employee, User, UserAuditLog
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


def _write_audit(db, action: str, *, target, ru, details: str,
                 target_label: str | None = None) -> None:
    """Append a view-as audit row. ``target`` is the impersonated *User* for
    USER view-as (its id is recorded in target_user_id, a FK to users.id).
    For EMPLOYEE/DRIVER view-as there is no User row, so pass ``target=None``
    and an explicit ``target_label`` (e.g. 'employee:Jane Doe') -- the label is
    stored verbatim and target_user_id stays NULL (it must, the FK only points
    at users.id)."""
    if target_label is not None:
        label = target_label[:120]
    elif target is not None:
        label = (f"view_as:{target.full_name}")[:120]
    else:
        label = "view_as:stop"
    try:
        db.add(UserAuditLog(
            target_user_id=(target.id if target else None),
            target_label=label,
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
    # If an employee/driver swap is in flight, USER view-as can't take effect
    # (load_current_user's no-uid branch never honors view_as_user_id) -> exit
    # the swap first rather than layering a junk key.
    if session.get("view_as_owner_uid") is not None:
        return redirect(url_for("view_as.view_as_stop"))
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


# ---------------------------------------------------------------------------
# EMPLOYEE / DRIVER view-as: owner-anchored session SWAP.
#
# Unlike USER view-as (which keeps the owner's user_id and only re-points
# g.current_user), the employee/driver self-views read session['employee_id']
# / session['driver_id'] DIRECTLY. To see a person's ACTUAL login view we
# therefore SWAP the session to be that employee/driver, while stashing the
# real owner (view_as_owner_uid + its session_version) so:
#   * permissions.load_current_user can recover g.real_user + g.viewing_as
#     even though user_id is popped (the partner re-check fails closed),
#   * the read-only flush guard (keyed on g.viewing_as) stays armed,
#   * the owner-gated /view-as control routes stay reachable + owner-gated,
#   * /view-as/stop can restore the owner's management session.
# Popping user_id makes the session a PURE employee/driver -> the
# employee->/partner firewall correctly blocks /partner/* (no escalation),
# and impersonating_user_id is NEVER set.
# ---------------------------------------------------------------------------
def _start_principal_view_as(*, kind: str, target_id: int,
                             name: str, session_version,
                             id_key: str, sv_key: str,
                             extra_session: dict, redirect_to: str):
    """Shared owner-anchored swap for employee/driver view-as. The caller has
    already owner-gated + loaded/validated the target. ``kind`` is
    'employee'|'driver'; ``id_key``/``sv_key`` are that principal's session
    keys; ``extra_session`` carries any additional keys to set on the swapped
    session (e.g. driver_name/driver_location)."""
    ru = _real_user()
    db = SessionLocal()
    try:
        # Anchor the REAL owner so gating/exit survive the swap. (ru is a
        # partner -- _require_owner guaranteed it before we got here.)
        session["view_as_owner_uid"] = ru.id
        session["view_as_owner_sv"] = ru.session_version
        session["view_as_kind"] = kind
        # Swap the session to BE the target principal. Pop the owner's User
        # keys so the session reads as a pure employee/driver (firewall-correct,
        # no /partner access). impersonating_user_id is intentionally NOT set.
        session.pop("user_id", None)
        session.pop("user_session_version", None)
        # CRITICAL: a real employee/driver login ALWAYS pops partner_auth_ok
        # (employee_auth._establish_employee_session). Leaving it set would let
        # the swapped session pass the employee->/partner firewall and reach
        # partner-only routes (privilege escalation). The owner re-enters the
        # /partner second factor after /view-as/stop -- intentional + safe.
        session.pop("partner_auth_ok", None)
        session.pop(VIEW_AS_SESSION_KEY, None)   # not a USER view-as
        session.pop(IMPERSONATE_SESSION_KEY, None)
        # Pop BOTH principals' keys so the swap session is a PURE employee/driver
        # with NO residual cross-kind key. A linked-employee partner (UNIFY fold)
        # carries their OWN employee_id alongside user_id; without this, a DRIVER
        # swap would leave that employee_id behind and the loader's cross-kind
        # guard would misread it as a foreign principal and refuse to revive the
        # genuine view-as. The bound kind's keys are (re)set immediately below.
        for _pk in ("employee_id", "employee_session_version",
                    "driver_id", "driver_name", "driver_location",
                    "driver_session_version"):
            session.pop(_pk, None)
        session[id_key] = target_id
        session[sv_key] = session_version
        # Bind the anchor to THIS planted principal so a foreign re-login on a
        # shared device (a different employee_id/driver_id) cannot inherit the
        # owner anchor: load_current_user only revives view-as when the present
        # principal == view_as_principal_id, else it tears the anchor down.
        session["view_as_principal_id"] = target_id
        for k, v in (extra_session or {}).items():
            session[k] = v
        session["auth_ok"] = True   # passes the site gate; does NOT grant partner
        session.permanent = True
        _write_audit(
            db, "view_as_start", target=None, ru=ru,
            target_label=f"{kind}:{name}",
            details=f"real={ru.id if ru else '?'} -> view_as {kind}={target_id} ({name})",
        )
    finally:
        db.close()
    return redirect(redirect_to)


@view_as_bp.route("/view-as/employee/<int:employee_id>", methods=["POST"])
def view_as_employee_start(employee_id: int):
    """Open an EMPLOYEE's actual logged-in self-view (read-only). Owner-gated on
    the REAL partner (resolved via g.real_user, which survives the swap through
    view_as_owner_uid). Missing/inactive target -> 404 with NO swap."""
    gate = _require_owner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        emp = db.query(Employee).filter(Employee.id == employee_id).first()
        if emp is None or not emp.active:
            return ("That employee was not found or is inactive.", 404)
        name = (emp.full_name or "").strip() or f"employee #{emp.id}"
        sv = emp.session_version
    finally:
        db.close()
    return _start_principal_view_as(
        kind="employee", target_id=employee_id, name=name,
        session_version=sv, id_key="employee_id", sv_key="employee_session_version",
        extra_session={}, redirect_to="/employee/my-profile",
    )


@view_as_bp.route("/view-as/driver/<int:driver_id>", methods=["POST"])
def view_as_driver_start(driver_id: int):
    """Open a DRIVER's actual logged-in self-view (read-only) at /my-profile.
    Owner-gated on the REAL partner. Missing/inactive target -> 404 with NO
    swap. A clean driver self-view exists (driver_system.my_profile reads
    session['driver_id']), so drivers mirror the employee swap exactly."""
    gate = _require_owner()
    if gate is not None:
        return gate
    db = SessionLocal()
    try:
        drv = db.query(Driver).filter(Driver.id == driver_id).first()
        if drv is None or not drv.active:
            return ("That driver was not found or is inactive.", 404)
        name = (drv.name or "").strip() or f"driver #{drv.id}"
        sv = drv.session_version
        extra = {"driver_name": drv.name, "driver_location": drv.location}
    finally:
        db.close()
    return _start_principal_view_as(
        kind="driver", target_id=driver_id, name=name,
        session_version=sv, id_key="driver_id", sv_key="driver_session_version",
        extra_session=extra, redirect_to="/my-profile",
    )


@view_as_bp.route("/view-as/stop", methods=["GET", "POST"])
def view_as_stop():
    # Always permitted so the owner can ALWAYS exit, even mid-impersonation
    # and even though the read-only guard is active. NOT under /partner, so the
    # employee/driver firewall never blocks the exit.
    # Resolve the real owner for the audit BEFORE we tear down the anchor
    # (_real_user reads view_as_owner_uid during an employee/driver swap).
    ru = _real_user()
    owner_uid = session.get("view_as_owner_uid")
    if owner_uid is not None and session.get("user_id") is None:
        # EMPLOYEE/DRIVER view-as: restore the partner's MANAGEMENT session and
        # drop the swapped employee/driver identity entirely. (Only when user_id
        # is ABSENT -- a true swap -- so a stale anchor can never overwrite a
        # live management user_id.)
        owner_sv = session.get("view_as_owner_sv")
        kind = session.get("view_as_kind")
        session["user_id"] = owner_uid
        if owner_sv is not None:
            session["user_session_version"] = owner_sv
        for k in ("view_as_owner_uid", "view_as_owner_sv", "view_as_kind",
                  "view_as_principal_id",
                  "employee_id", "employee_session_version",
                  "driver_id", "driver_name", "driver_location",
                  "driver_session_version",
                  VIEW_AS_SESSION_KEY, IMPERSONATE_SESSION_KEY):
            session.pop(k, None)
        session.permanent = True
        db = SessionLocal()
        try:
            _write_audit(db, "view_as_stop", target=None, ru=ru,
                         details=f"stopped {kind or 'principal'} view-as; restored owner={owner_uid}")
        finally:
            db.close()
        return redirect("/")
    # Defensive: if an owner-anchor coexists with a live user_id (should not --
    # load_current_user clears it on the uid path), drop the stray anchor
    # WITHOUT touching the live session.
    for k in ("view_as_owner_uid", "view_as_owner_sv", "view_as_kind",
              "view_as_principal_id"):
        session.pop(k, None)
    # USER view-as (or nothing active): the original behavior.
    had = session.pop(VIEW_AS_SESSION_KEY, None)
    session.pop(IMPERSONATE_SESSION_KEY, None)
    if had is not None:
        db = SessionLocal()
        try:
            _write_audit(db, "view_as_stop", target=None, ru=ru, details=f"stopped view_as={had}")
        finally:
            db.close()
    return redirect("/")


def _principal_view_as_name(kind: str):
    """Resolve the swapped EMPLOYEE/DRIVER's display name from the swapped
    session id (a single PK lookup, only on the rare viewing-as page render).
    g.current_user is NOT the target in this mode. Returns a raw (unescaped)
    name string; the caller escapes."""
    db = SessionLocal()
    try:
        if kind == "employee":
            eid = session.get("employee_id")
            row = db.query(Employee).filter(Employee.id == eid).first() if eid else None
            return (getattr(row, "full_name", None) or "").strip() or "employee"
        did = session.get("driver_id")
        row = db.query(Driver).filter(Driver.id == did).first() if did else None
        return (getattr(row, "name", None) or "").strip() or "driver"
    finally:
        db.close()


def _view_as_banner_html() -> str:
    """Sticky red banner shown on every page while impersonating. Returns ''
    when not viewing-as. Names are escaped (defense against stored markup)."""
    if not getattr(g, "viewing_as", False):
        return ""
    try:
        stop = url_for("view_as.view_as_stop")
    except Exception:
        stop = "/view-as/stop"
    # EMPLOYEE/DRIVER view-as: the session was swapped, so describe the swapped
    # principal (g.current_user is not the target here).
    kind = session.get("view_as_kind")
    if kind in ("employee", "driver"):
        name = escape(_principal_view_as_name(kind))
        inner = (f'&#128065; VIEW-AS (read-only) &mdash; seeing the app as '
                 f'<b>{name}</b> ({kind})')
    else:
        target = getattr(g, "current_user", None)
        name = escape(getattr(target, "full_name", "user"))
        role = escape(getattr(target, "permission_level", "?"))
        inner = f'&#128065; VIEW-AS (read-only) &mdash; seeing the app as <b>{name}</b> ({role})'
    return (
        '<div style="position:sticky;top:0;z-index:99999;background:#b00020;color:#fff;'
        'padding:6px 14px;font:600 14px/1.4 system-ui,Segoe UI,sans-serif;display:flex;'
        'gap:12px;justify-content:space-between;align-items:center;">'
        f'<span>{inner}</span>'
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

    # Belt-and-suspenders banner injection for HTML pages that do NOT extend
    # base_dashboard.html (and therefore never render the {{ view_as_banner }}
    # context var) -- chiefly the standalone EMPLOYEE self-view pages
    # (/employee/my-profile, /employee/dashboard, ...). Without this the owner
    # could be viewing-as an employee with NO visible banner/exit, defeating
    # the "always-visible exit" safety promise. We splice the banner right
    # after <body> ONLY when viewing-as AND the marker isn't already present
    # (so base_dashboard.html / driver pages, which already render it via the
    # context processor, are never double-bannered). Fail-safe: any hiccup
    # leaves the response untouched.
    _BANNER_MARKER = "VIEW-AS (read-only)"

    @app.after_request
    def _inject_view_as_banner_into_html(resp):
        try:
            if not getattr(g, "viewing_as", False):
                return resp
            ctype = (resp.content_type or "")
            if "text/html" not in ctype:
                return resp
            if resp.direct_passthrough:
                return resp
            body = resp.get_data(as_text=True)
            if _BANNER_MARKER in body:
                return resp  # already rendered via the context processor
            banner = _view_as_banner_html()
            if not banner:
                return resp
            lower = body.lower()
            idx = lower.find("<body")
            if idx == -1:
                return resp
            close = body.find(">", idx)
            if close == -1:
                return resp
            new_body = body[: close + 1] + banner + body[close + 1:]
            resp.set_data(new_body)
        except Exception:
            current_app.logger.exception("view_as banner injection failed")
        return resp
