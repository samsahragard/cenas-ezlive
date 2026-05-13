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
    deactivate)."""
    from app.db import SessionLocal
    from app.models import User

    uid = current_user_id()
    if not uid:
        g.current_user = None
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
            g.current_user = None
            return None
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
