"""Permission system — Phase 0 Block 4 (ck, 2026-05-13).

Implementation of samai's permission_system spec at
/partner/developer/app/permission-system (dfde3de).

Three things this module gives the rest of the app:

  1. ROLE_PERMISSIONS — the dict lifted verbatim from §3.1 of the spec.
     One entry per role, value is the set of permission tags granted.
     Partner's set is the literal wildcard {"*"} so the decorator can
     short-circuit any check without enumerating 60 tags.

  2. requires_permission(tag, *, store_arg=None, scope=None) — a route
     decorator. Reads g.current_user, calls _user_has, redirects to
     auth.access_denied?need=<tag> on failure. Scope qualifier is
     forwarded to the handler via g.permission_scope (the handler
     interprets it; the decorator just makes it available).

  3. has_permission(tag, store_id=None) — Jinja-side gate. Registered
     as a global so templates can write
     {% if has_permission('legal.view') %}…{% endif %} to hide UI
     elements the user can't operate.

Enforcement mode (flipped 2026-05-13 per Sam's option-(b) directive, after
Team UI populated the User table — distinct-risk-surface commit from the
Team UI itself, samai re-runs her checklist on this commit specifically):
  Default is now ENFORCING — a denial redirects the request to
  auth.access_denied with the missing tag in the query string. To
  temporarily disable enforcement (e.g. during a future schema migration
  where role mappings are mid-flux), set PERMISSION_ENFORCE=0 in env;
  the module falls back to the original dark-launch log-and-permit
  behavior. Any value other than the literal "0" — including unset —
  means enforcement is on.

  Logged denials go to the standard logger at WARN level so they're
  visible in Sentry / stdout without spamming.

Legacy User.permission_level values that aren't in the spec's taxonomy
yet ('manager' as a generic value, 'corporate-driver' instead of
'driver') get back-compat aliases below so the dark-launch period
doesn't silently deny existing users. The migration path renames
those values in the users table later.
"""
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Callable

from flask import g, redirect, request, session, url_for

log = logging.getLogger(__name__)


# ============================================================
# §3.1 — Role → permission mapping (lifted verbatim from the spec).
# ============================================================
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "partner": {"*"},

    "corporate": {
        "labor.view_own_wage", "labor.view_others_wage",
        "labor.view_own_hours", "labor.view_others_hours",
        "labor.view_boh_costs", "labor.view_foh_costs",
        "labor.view_store_summary", "labor.view_all_stores",
        "sales.view_today", "sales.view_history", "sales.view_by_channel",
        "sales.view_all_stores", "sales.export",
        "orders.view", "orders.view_history",
        "orders.assign_driver", "orders.unassign_driver",
        "orders.mark_picked_up", "orders.mark_delivered",
        "orders.edit_attachments", "orders.view_payout",
        "drivers.admin", "drivers.reset_passcode", "drivers.view_roster",
        "produce.order", "produce.invoice_verify",
        "produce.view_quotes", "produce.view_vendor_list",
        "produce.upload_invoice", "produce.dispute",
        "manager_log.write", "manager_log.read_own_store",
        "manager_log.read_all_stores", "manager_log.delete",
        "ai.ask_claude", "ai.ask_claude_personal", "ai.view_transcripts",
        "email.view_own_mailbox", "email.view_shared_mailbox", "email.send",
        "transcripts.search", "transcripts.read",
        "kds.view_alerts", "kds.view_kitchen_display", "kds.edit_recipes",
        "developer.view_chat", "developer.view_app_docs",
        "access.approve_request", "access.deny_request",
    },

    "gm": {
        "labor.view_own_wage", "labor.view_others_wage",
        "labor.view_own_hours", "labor.view_others_hours",
        "labor.view_boh_costs", "labor.view_foh_costs",
        "labor.view_store_summary",
        "sales.view_today", "sales.view_history", "sales.view_by_channel",
        "orders.view", "orders.view_history",
        "orders.assign_driver", "orders.unassign_driver",
        "orders.mark_picked_up", "orders.mark_delivered",
        "orders.edit_attachments", "orders.view_payout",
        "drivers.admin", "drivers.reset_passcode", "drivers.view_roster",
        "produce.order", "produce.invoice_verify",
        "produce.view_quotes", "produce.view_vendor_list",
        "produce.upload_invoice", "produce.dispute",
        "manager_log.write", "manager_log.read_own_store",
        "manager_log.delete",
        "ai.ask_claude", "ai.ask_claude_personal", "ai.view_transcripts",
        "email.view_own_mailbox", "email.view_shared_mailbox", "email.send",
        "transcripts.search", "transcripts.read",
        "kds.view_alerts", "kds.view_kitchen_display",
    },

    "km": {
        "labor.view_own_wage", "labor.view_own_hours",
        "labor.view_others_hours",  # read-only on roster
        "labor.view_boh_costs", "labor.view_store_summary",
        "sales.view_today", "sales.view_history",
        "orders.view", "orders.view_history",
        "orders.assign_driver", "orders.unassign_driver",
        "orders.mark_picked_up", "orders.mark_delivered",
        "orders.edit_attachments",
        "produce.order", "produce.invoice_verify",
        "produce.view_quotes", "produce.view_vendor_list",
        "produce.upload_invoice", "produce.dispute",
        "manager_log.write", "manager_log.read_own_store",
        "ai.ask_claude", "ai.ask_claude_personal", "ai.view_transcripts",
        "email.view_own_mailbox", "email.view_shared_mailbox", "email.send",
        "transcripts.search", "transcripts.read",
        "kds.view_alerts", "kds.view_kitchen_display", "kds.edit_recipes",
    },

    "assistant_km": {
        "labor.view_own_wage", "labor.view_own_hours",
        "labor.view_boh_costs",  # read-only
        "labor.view_store_summary",
        "sales.view_today", "sales.view_history",
        "orders.view", "orders.view_history",
        "orders.assign_driver", "orders.unassign_driver",
        "orders.mark_picked_up", "orders.mark_delivered",
        "orders.edit_attachments",
        "produce.view_quotes", "produce.view_vendor_list",
        "produce.upload_invoice",
        "manager_log.write", "manager_log.read_own_store",
        "ai.ask_claude", "ai.ask_claude_personal", "ai.view_transcripts",
        "email.view_own_mailbox", "email.view_shared_mailbox", "email.send",
        "transcripts.search", "transcripts.read",
        "kds.view_alerts", "kds.view_kitchen_display",
    },

    "corporate_chef": {
        # multi-store role; scope='all_stores' on every store-scoped tag
        "labor.view_own_wage", "labor.view_others_wage",
        "labor.view_own_hours", "labor.view_others_hours",
        "labor.view_boh_costs", "labor.view_foh_costs",
        "labor.view_store_summary", "labor.view_all_stores",
        "sales.view_today", "sales.view_history",
        "orders.view", "orders.view_history",
        "orders.assign_driver", "orders.unassign_driver",
        "orders.mark_picked_up", "orders.mark_delivered",
        "orders.edit_attachments",
        "produce.order", "produce.invoice_verify",
        "produce.view_quotes", "produce.view_vendor_list",
        "produce.upload_invoice", "produce.dispute",
        "manager_log.write", "manager_log.read_own_store",
        "manager_log.read_all_stores",
        "ai.ask_claude", "ai.ask_claude_personal", "ai.view_transcripts",
        "email.view_own_mailbox", "email.view_shared_mailbox", "email.send",
        "transcripts.search", "transcripts.read",
        "kds.view_alerts", "kds.view_kitchen_display", "kds.edit_recipes",
    },

    "prep_manager": {
        # multi-store; produce receive (not order)
        "labor.view_own_wage", "labor.view_own_hours",
        "labor.view_others_hours",  # direct reports only (decorator-enforced)
        "labor.view_all_stores",
        "orders.view", "orders.view_history",
        "orders.mark_picked_up", "orders.mark_delivered",
        "produce.invoice_verify",  # receive + verify, no place-order
        "produce.view_quotes", "produce.view_vendor_list",
        "produce.upload_invoice",
        "manager_log.write", "manager_log.read_own_store",
        "manager_log.read_all_stores",
        "ai.ask_claude", "ai.ask_claude_personal", "ai.view_transcripts",
        "email.view_own_mailbox", "email.view_shared_mailbox", "email.send",
        "transcripts.search", "transcripts.read",
        "kds.view_alerts", "kds.view_kitchen_display",
    },

    "foh_manager": {
        "labor.view_own_wage", "labor.view_own_hours",
        "labor.view_others_hours",  # FOH staff only
        "labor.view_foh_costs", "labor.view_store_summary",
        "sales.view_today", "sales.view_history", "sales.view_by_channel",
        "orders.view", "orders.view_history",
        "orders.assign_driver", "orders.unassign_driver",
        "orders.mark_picked_up", "orders.mark_delivered",
        "orders.edit_attachments",
        "manager_log.write", "manager_log.read_own_store",
        "ai.ask_claude", "ai.ask_claude_personal", "ai.view_transcripts",
        "email.view_own_mailbox", "email.view_shared_mailbox", "email.send",
        "transcripts.search", "transcripts.read",
        "kds.view_alerts",  # FOH-side anomalies only
    },

    "expo": {
        "orders.view",
        "orders.mark_picked_up", "orders.mark_delivered",
        "orders.edit_attachments",
        "ai.ask_claude_personal", "ai.view_transcripts",
        "transcripts.search", "transcripts.read",
        "kds.view_kitchen_display",
    },

    "driver": {
        "labor.view_own_wage", "labor.view_own_hours",
        "orders.view",  # own bids only (scope on route)
        "orders.mark_picked_up", "orders.mark_delivered",  # own only
        "orders.view_payout",  # own bids only
        "drivers.bid", "drivers.view_own_history",
        "ai.ask_claude_personal", "ai.view_transcripts",
        "transcripts.search", "transcripts.read",
    },
}


# Back-compat aliases for legacy User.permission_level values that
# pre-date the spec's taxonomy. During dark-launch these keep existing
# users from being denied everywhere; after the migration the User
# rows get renamed to the canonical keys above.
_LEGACY_ALIASES = {
    # The generic 'manager' value pre-dates the km / foh_manager split.
    # Granting gm-equivalent set during dark-launch — the eventual
    # migration is per-user (manual review).
    "manager": "gm",
    # 'corporate-driver' is the User-side value for what the spec calls
    # 'driver'. Same permission set.
    "corporate-driver": "driver",
}


def _canonical_role(level: str | None) -> str | None:
    """Translate a User.permission_level value into a key the
    ROLE_PERMISSIONS dict knows about. None on absent / unknown."""
    if not level:
        return None
    if level in ROLE_PERMISSIONS:
        return level
    return _LEGACY_ALIASES.get(level)


# ============================================================
# Core check
# ============================================================
def _user_has(user, tag: str, store_id: str | None = None) -> bool:
    """Return True if `user` has `tag`. Wildcard "*" matches anything.

    user        — User row (from g.current_user) OR None / dict-like.
    tag         — the permission key.
    store_id    — when set, the user's assignment for that store must
                  cover the tag. For now User.store_scope is a single
                  CSV string ('dos,uno') rather than a separate
                  user_store_assignments table; we treat membership in
                  that scope set as having an assignment, and
                  partner/corporate (store_scope NULL) get all stores.

    Impersonation: if session.impersonating_user_id is set, evaluate as
    that user instead. Logged so the audit trail catches who's actually
    acting. Phase 1+ surface for managing impersonation grants.
    """
    if user is None and not session.get("partner_auth_ok"):
        return False

    # Honor impersonation if present
    impersonating = session.get("impersonating_user_id")
    if impersonating and user and user.id != impersonating:
        # The session is impersonating someone else; evaluate as that
        # user. The audit log captures both the real id (user.id) and
        # the effective id (impersonating).
        try:
            from app.db import SessionLocal
            from app.models import User as _U
            db = SessionLocal()
            try:
                effective = db.get(_U, impersonating)
            finally:
                db.close()
            if effective is not None:
                user = effective
        except Exception:
            # If the lookup fails fall back to the original user — fail
            # closed in the deny direction.
            log.exception("impersonation user lookup failed")

    # Partner-Tier-2-only sessions (no User row) — treat as partner
    # for back-compat with existing partner_auth_ok-only flows.
    if user is None and session.get("partner_auth_ok"):
        return True

    role = _canonical_role(getattr(user, "permission_level", None))
    if role is None:
        return False
    perms = ROLE_PERMISSIONS.get(role, set())
    if "*" in perms:
        return True
    if tag not in perms:
        return False
    # Store-scope check — only enforced when the caller explicitly
    # passes store_id AND the user has a store_scope restriction.
    # Multi-store roles (partner / corporate / corporate_chef /
    # prep_manager / driver) have store_scope = None = all stores.
    if store_id is not None:
        scope = (getattr(user, "store_scope", None) or "").strip()
        if scope:
            assigned = {s.strip() for s in scope.split(",") if s.strip()}
            if store_id not in assigned:
                return False
    return True


def _enforcing() -> bool:
    """Enforcement toggle. Default True = deny + redirect to /access-denied.
    Set PERMISSION_ENFORCE=0 in env to fall back to dark-launch
    (log + permit) — useful during a role-schema migration where a
    transient mismatch might deny legitimate users until the new mappings
    settle. Any non-"0" value (including unset) means enforce.

    Default flipped 2026-05-13 per Sam (option-(b) of the timing question
    asked in 8b5b32b commit-post — wait for team populated, then ship the
    flip as its own commit + review surface)."""
    return os.getenv("PERMISSION_ENFORCE", "1") != "0"


def _log_denial(user, tag: str, store_id: str | None, route: str) -> None:
    log.warning(
        "permission denial: route=%s tag=%s user_id=%s role=%s store_id=%s mode=%s",
        route, tag,
        (user.id if user else "(tier2-only)"),
        (getattr(user, "permission_level", "(none)") if user else "(none)"),
        store_id,
        "ENFORCING" if _enforcing() else "DARK-LAUNCH",
    )


# ============================================================
# Public API — decorator + Jinja helper
# ============================================================
def requires_permission(tag: str, *, store_arg: str | None = None,
                        scope: str | None = None) -> Callable:
    """Route decorator. See module docstring.

    tag        — permission key.
    store_arg  — route kwarg name carrying the store id; if set, the
                 user's assignment for that store is checked.
    scope      — qualifier forwarded to handler via g.permission_scope.
                 The decorator doesn't enforce scope; the handler does.
    """
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = getattr(g, "current_user", None)
            store_id = kwargs.get(store_arg) if store_arg else None
            ok = _user_has(user, tag, store_id)
            if not ok:
                _log_denial(user, tag, store_id, request.path)
                if _enforcing():
                    # Cleanly redirect to the access-denied page.
                    return redirect(url_for(
                        "auth.access_denied", need=tag,
                        next=request.path))
                # Dark-launch — log and pass through. The decorator
                # still sets g.permission_scope below so handlers
                # can act on it.
            g.permission_scope = scope
            return fn(*args, **kwargs)
        return wrapped
    return deco


def has_permission(tag: str, store_id: str | None = None) -> bool:
    """Public check — used by code that's not behind the decorator
    (e.g. conditional logic inside a handler). Same shape as
    has_permission_jinja but without the Jinja-context binding."""
    return _user_has(getattr(g, "current_user", None), tag, store_id)


def has_permission_jinja(tag: str, store_id: str | None = None,
                         store: str | None = None) -> bool:
    """Jinja-exposed helper. Accepts either store_id= or store= as the
    keyword (templates use the shorter form per the spec example)."""
    return _user_has(getattr(g, "current_user", None),
                     tag, store_id or store)


def install(app):
    """Register the Jinja global. Call from create_app after the
    keypad blueprint is installed so g.current_user is reachable."""
    app.jinja_env.globals["has_permission"] = has_permission_jinja
