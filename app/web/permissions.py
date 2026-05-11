"""Permission helpers for the User-based keypad auth.

Hierarchy (high → low privilege):
    partner > corporate > gm > manager > expo > corporate-driver

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

LEVELS = ("partner", "corporate", "gm", "manager", "expo", "corporate-driver")
_LEVEL_IDX = {name: i for i, name in enumerate(LEVELS)}


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
