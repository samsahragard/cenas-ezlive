"""Permission helpers for the User-based keypad auth.

Hierarchy (high → low privilege, post-Block-4 Team-UI expansion 2026-05-13):
    partner > corporate > corporate_chef > gm > km > assistant_km
    > prep_manager > foh_manager > expo > driver

Two legacy aliases ('manager' = gm, 'corporate-driver' = driver) are
retained as a safety net so any stale rows that pre-date samai's
permission_system spec still pass require_level checks. The aliases are
ranked alongside their canonical counterpart. Phase 2 cleanup drops them
once we confirm zero stale rows survive in production. See the
ROLE_PERMISSIONS dict in app/services/permissions.py for the
tag-based check that supersedes this ladder once PERMISSION_ENFORCE=1
flips on.

`require_level(min_level)` is a Flask view decorator that 302's
to /keypad-login (saving ?next=) if no user is signed in, returns 403
if the signed-in user's level is below the minimum, and otherwise lets
the view run. The current user is stashed on `g.current_user` so views
can use it without another DB hit.
"""
from __future__ import annotations

from functools import wraps
from typing import Callable

from flask import g, redirect, request, session, url_for

# Canonical ranking (high → low privilege). New roles slot in alongside
# their nearest legacy peer; legacy aliases sit immediately after their
# canonical replacement so old User rows keep their effective rank.
LEVELS = (
    "partner",
    "corporate",
    "corporate_chef",
    "gm",
    "manager",         # legacy alias for gm — remove in Phase 2 cleanup
    "km",
    "assistant_km",
    "prep_manager",
    "foh_manager",
    "expo",
    "driver",
    "corporate-driver",  # legacy alias for driver — remove in Phase 2 cleanup
)
_LEVEL_IDX = {name: i for i, name in enumerate(LEVELS)}

# Levels that are scoped to one or more stores (vs everywhere). Partner
# and corporate are intentionally absent — they see every store implicitly
# and their store_scope column stays NULL. Everyone else must carry at
# least one store assignment in User.store_scope (CSV: "tomball" /
# "copperfield" / "tomball,copperfield").
STORE_SCOPED_LEVELS = (
    "gm", "manager",
    "km", "assistant_km",
    "corporate_chef", "prep_manager", "foh_manager",
    "expo",
    "driver", "corporate-driver",
)

# Maps the User.store_scope CSV value to the store_slug each scope owns.
SCOPE_TO_SLUG = {
    "tomball":     "dos",
    "copperfield": "uno",
}


def accessible_store_slugs(user) -> list[str]:
    """Return the store slugs ('dos' / 'uno' / 'corporate' / 'partner') this
    user can access. Partner and corporate see everything via their own
    slug; store-scoped roles (gm, manager, km, assistant_km, corporate_chef,
    prep_manager, foh_manager, expo, driver, corporate-driver) get one entry
    per assigned store, derived from the User.store_scope CSV. Order is
    stable so the sidebar dropdown is consistent."""
    if user is None:
        return []
    level = user.permission_level
    if level == "partner":
        return ["partner"]
    if level == "corporate":
        return ["corporate"]
    if level == "corporate-driver":
        return []
    # Everyone else (gm/manager/km/assistant_km/corporate_chef/prep_manager/
    # foh_manager/expo/driver): split the CSV store_scope into slugs.
    out: list[str] = []
    for scope in (user.store_scope or "").split(","):
        slug = SCOPE_TO_SLUG.get(scope.strip())
        if slug and slug not in out:
            out.append(slug)
    return out


def level_rank(level: str | None) -> int:
    """Lower number = higher privilege. Unknown levels rank last."""
    if level is None:
        return len(LEVELS)
    return _LEVEL_IDX.get(level, len(LEVELS))


def level_at_least(user_level: str | None, min_level: str) -> bool:
    return level_rank(user_level) <= level_rank(min_level)


def current_user_id() -> int | None:
    uid = session.get("user_id")
    try:
        return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


def load_current_user():
    """Stash the current User on g.current_user (or None if no session).
    Force-logs-out the session if the user is inactive OR if their
    session_version has been bumped since login (passcode reset / admin
    deactivate).

    Also resolves the REAL owner + g.viewing_as for BOTH view-as modes:
      * USER view-as (session['view_as_user_id']): user_id stays set, so the
        owner is the user_id User and g.current_user is swapped to the target
        User (this function's original behavior).
      * EMPLOYEE/DRIVER view-as (session['view_as_owner_uid']): the owner's
        user_id was POPPED so the session reads as a pure employee/driver (the
        employee->/partner firewall correctly treats it that way and the
        employee/driver routes render the target). The real owner is recovered
        here from view_as_owner_uid so the banner, the read-only flush guard,
        the audit, and the owner-gated /view-as control routes all keep working.
        g.current_user is NOT swapped (those routes key off
        session['employee_id'] / session['driver_id'], not g.current_user)."""
    from app.db import SessionLocal
    from app.models import User

    g._view_as_loaded = True
    uid = current_user_id()
    if not uid:
        # No user_id. This is either a normal anon/employee/driver session OR
        # an active EMPLOYEE/DRIVER view-as (where the owner's user_id was
        # popped and stashed in view_as_owner_uid). Recover the real owner so
        # gating / banner / audit / read-only survive the swap.
        g.current_user = None
        g.real_user = None
        g.viewing_as = False
        owner_uid = session.get("view_as_owner_uid")
        if owner_uid:
            db = SessionLocal()
            try:
                owner = db.query(User).filter(User.id == owner_uid).first()
                _owner_ok = (owner is not None and owner.active
                             and session.get("view_as_owner_sv") == owner.session_version
                             and level_at_least(owner.permission_level, "partner"))
                _kind = session.get("view_as_kind")
                _bound = session.get("view_as_principal_id")
                if _kind == "employee":
                    _present_pid = session.get("employee_id")
                    _cross_present = session.get("driver_id") is not None
                    _planted_keys = ("employee_id", "employee_session_version")
                elif _kind == "driver":
                    _present_pid = session.get("driver_id")
                    _cross_present = session.get("employee_id") is not None
                    _planted_keys = ("driver_id", "driver_session_version",
                                     "driver_name", "driver_location")
                else:
                    _present_pid = None
                    _cross_present = True
                    _planted_keys = ()
                _anchor_keys = ("view_as_owner_uid", "view_as_owner_sv",
                                "view_as_kind", "view_as_principal_id",
                                "impersonating_user_id")
                if not _owner_ok:
                    # OWNER anchor revoked/bumped/not-partner. FAIL CLOSED: the
                    # employee/driver session keys exist ONLY because the view-as
                    # swap planted them, so they die WITH the anchor -> the session
                    # drops to anonymous (next request -> login). Leaving them would
                    # silently promote a just-revoked owner into a live, read-WRITE
                    # employee/driver self-session (no banner, no read-only guard).
                    for k in (*_anchor_keys, "employee_id", "employee_session_version",
                              "driver_id", "driver_name", "driver_location",
                              "driver_session_version", "auth_ok"):
                        session.pop(k, None)
                elif (_present_pid is not None and _present_pid == _bound
                          and not _cross_present):
                    # Owner valid AND the present principal of the bound KIND IS the
                    # swap target AND NO foreign cross-kind principal is also present
                    # -> genuine active view-as. Recover the owner so the banner,
                    # read-only flush guard, audit, and owner-gated /view-as control
                    # routes all survive the swap.
                    g.real_user = owner
                    g.viewing_as = True
                else:
                    # Owner valid BUT this is NOT a genuine view-as: a FOREIGN
                    # employee/driver re-login on a shared device (same- OR
                    # CROSS-kind), or no principal. Always clear the anchor so the
                    # foreign login can neither show a phantom view-as nor hijack
                    # /view-as/stop into the owner's identity. ALSO clear the stale
                    # PLANTED principal key IF it still holds the swap target (a
                    # cross-kind leftover the foreign login never overwrote) so it
                    # cannot leak the target's data -- but KEEP the foreign login's
                    # OWN principal/auth keys (no surprise logout).
                    _to_pop = list(_anchor_keys)
                    if _present_pid is not None and _present_pid == _bound:
                        _to_pop += list(_planted_keys)
                    for k in _to_pop:
                        session.pop(k, None)
            finally:
                db.close()
        return None
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == uid).first()
        if (u is None or not u.active
                or session.get("user_session_version") != u.session_version):
            # Stale or revoked session — clear it.
            session.pop("user_id", None)
            session.pop("user_session_version", None)
            session.pop("auth_ok", None)
            session.pop("partner_auth_ok", None)
            session.pop("view_as_user_id", None)
            session.pop("impersonating_user_id", None)
            g.current_user = None
            g.real_user = None
            g.viewing_as = False
            return None
        g.real_user = u
        g.viewing_as = False
        # A live user_id session is NEVER an employee/driver swap (the swap pops
        # user_id). If an owner-anchor lingers here, clear it so a stale anchor
        # can't later hijack /view-as/stop into overwriting this live user_id
        # (cross-principal swap on a shared device).
        if session.get("view_as_owner_uid") is not None:
            for k in ("view_as_owner_uid", "view_as_owner_sv", "view_as_kind",
                      "view_as_principal_id"):
                session.pop(k, None)
        # Owner-only "view as" impersonation (read-only QA, Sam-directed).
        # Resolve the effective user to the target ONLY when the real user is
        # a partner; defense-in-depth on top of the partner-gated /view-as
        # control routes. g.real_user keeps the actual owner for the banner,
        # the read-only guard, and audit.
        view_as_id = session.get("view_as_user_id")
        if view_as_id and view_as_id != u.id and level_at_least(u.permission_level, "partner"):
            target = db.query(User).filter(User.id == view_as_id).first()
            if target is not None and target.active:
                g.current_user = target
                g.viewing_as = True
                return target
            # Target gone/inactive — drop the impersonation cleanly.
            session.pop("view_as_user_id", None)
            session.pop("impersonating_user_id", None)
        g.current_user = u
        return u
    finally:
        db.close()


def require_level(min_level: str) -> Callable:
    """Decorator: redirect to keypad-login if anonymous, 403 if under-leveled."""
    if min_level not in _LEVEL_IDX:
        raise ValueError(f"unknown permission level: {min_level!r}")

    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            u = getattr(g, "current_user", None) or load_current_user()
            if u is None:
                nxt = request.full_path if request.full_path else request.path
                return redirect(url_for("keypad_auth.login", next=nxt))
            if not level_at_least(u.permission_level, min_level):
                return ("Forbidden — your account doesn't have access to this page.", 403)
            return view(*args, **kwargs)
        return wrapper
    return deco
