"""Dashboard access helpers for store-scoped partner surfaces.

The permission catalog is position-first, but live manager accounts can still
be temporarily missing their Employee/position link while Sam cleans up roster
records. These helpers are strict when store-position data exists and fall back
to the User role only when that link data is absent, so route gates work now
without hiding real managers behind the cleanup backlog.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from flask import abort, g, session

from app.services.permission_catalog import default_role_map
from app.services.permissions import has_permission

STORE_SLUG_TO_KEY = {
    "dos": "tomball",
    "uno": "copperfield",
    "corporate": "__both__",
    "partner": "__both__",
}

ROLE_ALIASES = {
    "manager": "gm",
    "corporate-driver": "corporate_driver",
}


def _canonical_role(role: str | None) -> str | None:
    role_key = (role or "").strip().lower()
    if not role_key:
        return None
    return ROLE_ALIASES.get(role_key, role_key)


def dashboard_store_key(store_slug: str | None = None) -> str | None:
    slug = (
        store_slug
        or getattr(g, "current_store", None)
        or session.get("last_store_slug")
        or ""
    )
    return STORE_SLUG_TO_KEY.get(str(slug).strip().lower())


@contextmanager
def _dashboard_store_scope(store_slug: str | None = None) -> Iterator[None]:
    """Evaluate catalog permissions against the route's store for this check."""
    store_key = dashboard_store_key(store_slug)
    marker = object()
    previous = session.get("active_store", marker)
    if store_key:
        session["active_store"] = store_key
    try:
        yield
    finally:
        if previous is marker:
            session.pop("active_store", None)
        else:
            session["active_store"] = previous


def _role_default_allows(user, tag: str) -> bool:
    role = _canonical_role(getattr(user, "permission_level", None))
    if role is None:
        return False
    return tag in default_role_map().get(role, set())


def _linked_position_exists(user, store_key: str | None) -> bool:
    """True when this User has any linked EmployeePosition in this store."""
    if not store_key:
        return False
    uid = getattr(user, "id", None)
    if uid is None:
        return False

    cache = getattr(g, "_dashboard_position_cache", None)
    if cache is None:
        cache = {}
        g._dashboard_position_cache = cache
    key = (uid, store_key)
    if key in cache:
        return cache[key]

    try:
        from app.db import SessionLocal
        from app.models import Employee, EmployeePosition

        db = SessionLocal()
        try:
            emp = db.query(Employee).filter(Employee.user_id == uid).first()
            if emp is None:
                cache[key] = False
                return False
            q = db.query(EmployeePosition.id).filter(
                EmployeePosition.employee_id == emp.id
            )
            if store_key != "__both__":
                q = q.filter(EmployeePosition.store_key == store_key)
            cache[key] = q.first() is not None
            return cache[key]
        finally:
            db.close()
    except Exception:
        cache[key] = False
        return False


def has_dashboard_access(tag: str, store_slug: str | None = None) -> bool:
    """Return whether the current session may see a dashboard/catalog surface."""
    if not tag:
        return False
    with _dashboard_store_scope(store_slug):
        if has_permission(tag):
            return True

    user = getattr(g, "current_user", None)
    if user is None:
        return False
    if not _role_default_allows(user, tag):
        return False

    store_key = dashboard_store_key(store_slug)
    if _linked_position_exists(user, store_key):
        # Position data exists for this store, so the catalog answer above is
        # authoritative. Do not let a broad User role leak across stores.
        return False
    return True


def require_dashboard_access(tag: str, store_slug: str | None = None) -> None:
    if not has_dashboard_access(tag, store_slug):
        abort(403)


def current_role_is(role: str) -> bool:
    user = getattr(g, "current_user", None)
    return _canonical_role(getattr(user, "permission_level", None)) == role
