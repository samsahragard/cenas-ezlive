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
        # Phase 0 Block 4 follow-up (Sam 2026-05-13 20:01). The two
        # ck-coined tags from cc7c41a (sidebar / route migration) gate
        # global config surfaces: anomaly.admin = /partner/anomalies/rules
        # (rule severity + threshold tuning) and access.team_admin =
        # /partner/team (the Team UI for adding / editing users). Both
        # sit at the partner + corporate tier — system tuning above
        # store scope. gm and below operate within their store and
        # shouldn't be tuning system-wide rules or managing all users.
        # The parallel with developer.view_chat is exact: when future
        # corporate hires come in (a controller, an ops director) they
        # inherit these naturally via role membership rather than
        # per-person grant — the role-based-not-per-person principle
        # doing the work it's supposed to. Future new admin-surface
        # tags should follow the same pattern (add to corporate's set
        # in this block, not relitigate the audience question per tag).
        "anomaly.admin", "access.team_admin",
        # Phase 1 / Block 6 calibration C2 (aick 2026-05-13). Gates the
        # /partner/briefs/<brief_id> read endpoint + /feedback form;
        # the route handler additionally verifies brief.audience_user_id
        # == current_user.id so a corporate user can only ever see
        # their own brief. Partner wildcard reaches everyone's briefs
        # (oversight + the calibration panel includes Sam). When Phase 2
        # extends the panel to GM (Anna + Brittany), add this tag to
        # the gm set in this module — same role-based-not-per-person
        # inheritance pattern.
        "briefs.view_own",
        # Phase 2 / Block 1G (ck 2026-05-14). The team-reports tab at
        # /partner/team-reports/ — task-based personnel reports.
        #   team_reports.view            → the tab + reports 1–3.
        #     Held by corporate + gm (and partner via wildcard). GM
        #     access is store-scoped at the QUERY layer (1G §4): the
        #     tag grants the tab, the server-derived scope confines a
        #     GM to their own store — defense-in-depth, the same shape
        #     as the 251621f unassign-courier cross-check.
        #   team_reports.view_all_stores → report #4 (per-store
        #     comparison) + the unfiltered cross-store view. corporate
        #     only (partner via wildcard) — NOT in the gm set; that
        #     omission is the line that keeps cross-store reporting
        #     partner/corporate-tier. Mirrors the existing
        #     labor.view_store_summary vs labor.view_all_stores split.
        "team_reports.view", "team_reports.view_all_stores",
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
        # Phase 2 / Block 1G (ck 2026-05-14). A GM holds
        # team_reports.view — they reach the team-reports tab + reports
        # 1–3 — but their access is store-scoped at the QUERY layer
        # (1G §4): _derive_store_scope confines a GM to their own store
        # with no request-param override. A GM does NOT hold
        # team_reports.view_all_stores (corporate + partner only), so
        # report #4 + the cross-store view stay above their tier — the
        # query for it never even runs for a GM. This is the
        # labor.view_store_summary / labor.view_all_stores split
        # applied to team reports.
        "team_reports.view",
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
        # Sam #1063 (2026-05-26): drivers.admin opened to every non-driver
        # role so any store team can add / deactivate drivers; store scope
        # is enforced by the /<store>/drivers URL prefix + blueprint
        # before_request, so Tomball team only reaches Tomball drivers.
        "drivers.admin", "drivers.view_roster", "drivers.reset_passcode",
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
        # Sam #1063 (2026-05-26): drivers.admin opened to non-driver roles.
        "drivers.admin", "drivers.view_roster", "drivers.reset_passcode",
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
        # Sam #1063 (2026-05-26): drivers.admin opened to non-driver roles.
        "drivers.admin", "drivers.view_roster", "drivers.reset_passcode",
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

    "foh_manager": {
        "labor.view_own_wage", "labor.view_own_hours",
        "labor.view_others_hours",  # FOH staff only
        "labor.view_foh_costs", "labor.view_store_summary",
        "sales.view_today", "sales.view_history", "sales.view_by_channel",
        "orders.view", "orders.view_history",
        "orders.assign_driver", "orders.unassign_driver",
        "orders.mark_picked_up", "orders.mark_delivered",
        "orders.edit_attachments",
        # Sam #1063 (2026-05-26): drivers.admin opened to non-driver roles.
        "drivers.admin", "drivers.view_roster", "drivers.reset_passcode",
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
        # Sam #1063 (2026-05-26): drivers.admin opened to non-driver roles.
        "drivers.admin", "drivers.view_roster", "drivers.reset_passcode",
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

    # Phase 2 / Block 1 precondition (samai spec, Sam §5.3 Path A
    # 2026-05-14). The five hourly roles the task-assignment hierarchy
    # (1A can_assign_to) + the counseling cascade (2C) reference. The
    # point of these entries is taxonomy completeness — "everyone in the
    # system is in the role taxonomy" (Sam) — so _user_has resolves them
    # to a real set, not None, and the permission matrix test covers
    # them. The tag sets are MINIMAL BY DESIGN: the personal-AI +
    # transcripts baseline that expo + driver already share. Operational
    # tag refinement per hourly role (does a cook need
    # kds.view_kitchen_display like expo, does a server need orders.view,
    # etc.) is a deliberate follow-up once these roles have actual app
    # surfaces — see the precondition spec §6 Q1. Do NOT mistake the thin
    # set for an oversight.
    # Sam #1063 (2026-05-26): drivers.admin opened to every non-driver
    # role. The hourly-tier roles get the tag here too so when the user
    # taxonomy fills out, a cook or server can also add a driver from
    # their store. Store scope is auto-enforced by the /<store>/drivers
    # URL prefix + blueprint before_request.
    "cook":      {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read",
                  "drivers.admin", "drivers.view_roster", "drivers.reset_passcode"},
    "server":    {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read",
                  "drivers.admin", "drivers.view_roster", "drivers.reset_passcode"},
    "busser":    {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read",
                  "drivers.admin", "drivers.view_roster", "drivers.reset_passcode"},
    "host":      {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read",
                  "drivers.admin", "drivers.view_roster", "drivers.reset_passcode"},
    "bartender": {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read",
                  "drivers.admin", "drivers.view_roster", "drivers.reset_passcode"},

    # Schedules V2 / Block 1 (Sam #1742). The employee scheduling identity
    # (Tier-5). V2 scope tags are forward-declared here; the V2 routes get
    # gated with them as B2-B10 build each surface.
    "employee": {"ai.ask_claude_personal", "ai.view_transcripts",
                 "transcripts.search", "transcripts.read",
                 "schedule.view_own", "schedule.accept_decline",
                 "shift.offer", "shift.swap_propose",
                 "timeoff.request", "availability.set",
                 "announcement.view", "message.reply"},
    # In-house corporate driver - a DISTINCT role from the ezCater 'driver'
    # role (which stays hardcoded/untouched). Same driver scope; managed via
    # the Permissions admin page.
    "corporate_driver": {"labor.view_own_wage", "labor.view_own_hours",
                         "orders.view", "orders.mark_picked_up",
                         "orders.mark_delivered", "orders.view_payout",
                         "drivers.bid", "drivers.view_own_history",
                         "ai.ask_claude_personal", "ai.view_transcripts",
                         "transcripts.search", "transcripts.read"},
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
    # tag=None means no permission tag is required — the action is open
    # to every role; the store-scope check below still applies. Used by
    # requires_store_access (Sam 2026-05-20: unhook / free-up driver have
    # no role restriction, only store scope).
    if tag is not None and tag not in perms:
        return False
    # Store-scope check — only enforced when the caller explicitly
    # passes store_id AND the user has a store_scope restriction.
    # Multi-store roles (partner / corporate / corporate_chef / driver) have store_scope = None = all stores.
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
    mode = "ENFORCING" if _enforcing() else "DARK-LAUNCH"
    log.warning(
        "permission denial: route=%s tag=%s user_id=%s role=%s store_id=%s mode=%s",
        route, tag,
        (user.id if user else "(tier2-only)"),
        (getattr(user, "permission_level", "(none)") if user else "(none)"),
        store_id,
        mode,
    )
    # Persist to PermissionDenial so /partner/developer/app/denials can
    # render the trail (spec §5.3). Best-effort — a DB failure here must
    # not block the routing path. Worst case we lose a row from the
    # surface but the log line is still in Sentry/stdout.
    try:
        from app.db import SessionLocal
        from app.models import PermissionDenial
        from flask import request as _request
        db = SessionLocal()
        try:
            db.add(PermissionDenial(
                user_id=(user.id if user else None),
                user_label=(getattr(user, "full_name", None) if user else None),
                user_role=(getattr(user, "permission_level", None) if user else None),
                tag=tag,
                route=route,
                ip=(_request.remote_addr if _request else None),
                mode=mode,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("permission denial: failed to persist PermissionDenial row")


# ============================================================
# Public API — decorator + Jinja helper
# ============================================================
def requires_permission(tag: str | None, *, store_arg: str | None = None,
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
                _deny_tag = tag or "store-access"
                _log_denial(user, _deny_tag, store_id, request.path)
                if _enforcing():
                    # Cleanly redirect to the access-denied page.
                    return redirect(url_for(
                        "auth.access_denied", need=_deny_tag,
                        next=request.path))
                # Dark-launch — log and pass through. The decorator
                # still sets g.permission_scope below so handlers
                # can act on it.
            g.permission_scope = scope
            return fn(*args, **kwargs)
        return wrapped
    return deco


def requires_store_access(store_arg: str) -> Callable:
    """Route decorator for actions open to ANY logged-in user, limited
    only by store assignment — no permission tag.

    Per Sam 2026-05-20: 'unhook driver' / 'free up ezCater driver' have
    no role restriction. Anyone may run them, confined to their assigned
    store(s); multi-store roles (partner / corporate / driver / ...)
    reach every store, exactly as with the tagged decorator. Implemented
    as requires_permission with a None tag so the user / impersonation /
    store-scope handling stays shared in one place."""
    return requires_permission(None, store_arg=store_arg)


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
