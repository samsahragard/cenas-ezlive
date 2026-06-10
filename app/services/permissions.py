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
        "team_reports.view",  # Sam 2026-06-08 (1.1): Today > Task Reports, store-scoped (NOT view_all_stores)
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
        "team_reports.view",  # Sam 2026-06-08 (1.1): Today > Task Reports, store-scoped (NOT view_all_stores)
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
        "team_reports.view",  # Sam 2026-06-08 (1.1): Today > Task Reports, store-scoped (NOT view_all_stores)
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
        "team_reports.view",  # Sam 2026-06-08 (1.1): Today > Task Reports, store-scoped (NOT view_all_stores)
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

    # Expo is a MANAGEMENT-section role (role_buckets) -- Sam 2026-06-07: "Expo is
    # in the management team, she should get the management profile." Brought up from
    # the old near-hourly set to the FLOOR-MANAGEMENT baseline (mirrors assistant_km /
    # foh_manager: labor + sales view, manager log, orders, drivers, kds) so an Expo
    # actually gets management access -- not just dash.kitchen. Pairs with adding
    # "expo" to MGR_UP in permission_catalog.py (the dashboard/catalog side). The
    # partner still tunes each store on the Permissions page. ADDITIVE -> never locks
    # anyone out (only grants more than before).
    "expo": {
        "labor.view_own_wage", "labor.view_own_hours", "labor.view_others_hours",
        "labor.view_boh_costs", "labor.view_store_summary",
        "sales.view_today", "sales.view_history",
        "orders.view", "orders.view_history",
        "orders.assign_driver", "orders.unassign_driver",
        "orders.mark_picked_up", "orders.mark_delivered",
        "orders.edit_attachments",
        "drivers.admin", "drivers.view_roster", "drivers.reset_passcode",
        "manager_log.write", "manager_log.read_own_store",
        "ai.ask_claude", "ai.ask_claude_personal", "ai.view_transcripts",
        "email.view_own_mailbox", "email.view_shared_mailbox", "email.send",
        "transcripts.search", "transcripts.read",
        "kds.view_alerts", "kds.view_kitchen_display",
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
    # 2026-05-14). The hourly roles the task-assignment hierarchy
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
    # S6b (Sam, 2026-06-07): HOURLY is SELF-ONLY — no company permissions.
    # Sam's #1063 grant (drivers.admin / drivers.view_roster /
    # drivers.reset_passcode opened to every non-driver role) is REVERSED for
    # the hourly tier here: an hourly user (cook / server / busser / host /
    # bartender / training) must NOT hold any company / admin / labor / sales tag. Their
    # effective perms are the self-only surface ONLY — own profile + own rank,
    # which on the tag side is the personal-AI + transcripts baseline that expo
    # + driver already share (these tags read the actor's own data; they grant
    # no roster/admin/labor/sales reach). Driver administration stays with the
    # management + corporate tiers (km / assistant_km / corporate_chef /
    # foh_manager / expo / gm / corporate still hold drivers.* above). The
    # ezCater 'driver' role is a SEPARATE system entirely and is untouched.
    "cook":      {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read"},
    "server":    {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read"},
    "busser":    {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read"},
    "host":      {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read"},
    "training":  {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read"},
    "bartender": {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read"},
    # S6b uniformity (ck, 2026-06-07): cashier + well were the two hourly
    # roles MISSING from this dict while the other five carried the self-only
    # baseline — _user_has resolved them to set() (deny-all) rather than the
    # self-only surface. Added here with EXACTLY the same self-only baseline so
    # all hourly roles are identical and self-only (own profile + own
    # rank; the personal-AI + transcripts tags read only the actor's own data,
    # grant no roster/admin/labor/sales reach).
    "cashier":   {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read"},
    "well":      {"ai.ask_claude_personal", "ai.view_transcripts",
                  "transcripts.search", "transcripts.read"},

    # Schedules V2 / Block 1 (Sam #1742). The employee scheduling identity
    # (Tier-5). V2 scope tags are forward-declared here; the V2 routes get
    # gated with them as B2-B10 build each surface.
    "employee": {"ai.ask_claude_personal", "ai.view_transcripts",
                 "transcripts.search", "transcripts.read",
                 "schedule.view_own", "schedule.accept_decline",
                 "shift.offer", "shift.swap_propose",
                 "timeoff.request", "availability.set",
                 "announcement.view", "message.view", "message.reply"},
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


# ============================================================
# CATALOG_TO_TAGS — the catalog-key -> route-tag bridge (perms-rework Piece A).
# ============================================================
# This is the AUDITABLE boundary: ONLY the route tags named here can be
# REVOKED by an OFF catalog toggle. Every OTHER route tag stays role-only
# (the role baseline always passes through, never revocable by the catalog).
#
# The catalog (permission_catalog.py) and the route @requires_permission tags
# (ROLE_PERMISSIONS, above) are two DIFFERENT vocabularies. This dict wires the
# handful of catalog keys that map 1:1 onto a real route tag. Confident 1:1
# bindings ONLY — ambiguous catalog keys and route tags with no catalog key are
# DELIBERATELY excluded; they remain on the role baseline. Expandable in later
# passes as more bindings are verified.
CATALOG_TO_TAGS: dict[str, set[str]] = {
    "emp.reset_passcode": {"drivers.reset_passcode"},
    "legal.upload_docs": {"legal.upload_document"},
    "dash.dev_chat": {"developer.view_chat"},
    "legal.view_insurance": {"legal.view_insurance"},
}
# The set of route tags under catalog authority. A tag in here is
# catalog-controlled (OFF revokes); a tag NOT in here is role-only.
MANAGED_TAGS: set[str] = set().union(*CATALOG_TO_TAGS.values()) if CATALOG_TO_TAGS else set()


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


def effective_perms_for(position_keys, store_key, db=None):
    """The UNION of ON permissions across the given positions at a store
    (Sam #2426/#2435, Q1=union). position_keys = a person's permission-role
    keys (from their positions); store_key = the active store. Returns a set of
    perm_keys. The source of truth for position-based enforcement; never raises
    (returns set() on no positions / no store). Manages its own session if none
    is passed."""
    if not position_keys or not store_key:
        return set()
    from app.models import PositionPermission
    close = False
    if db is None:
        from app.db import SessionLocal
        db = SessionLocal()
        close = True
    try:
        rows = (db.query(PositionPermission.perm_key)
                .filter(PositionPermission.store_key == store_key,
                        PositionPermission.position_key.in_(list(position_keys)))
                .distinct().all())
        return {r[0] for r in rows}
    finally:
        if close:
            db.close()


# ============================================================
# Position-based effective perms (perms-rework, the repoint).
# ============================================================
def _effective_perms(user) -> set[str]:
    """Resolve `user`'s effective permission set for the ACTIVE store
    (session['active_store'], a LOCATION key tomball/copperfield), gating
    the saved position-union BEHIND the static role-perms fallback.

    Lockout-safe by construction (the 5-hole spec):
      * the partner wildcard is handled by the caller BEFORE this runs,
        off User.permission_level, so a partner can never self-lock;
      * role-perms = ROLE_PERMISSIONS.get(role, set()) is the real
        fallback and is returned whenever the active store is unset OR
        the person has no Employee OR no positions at that store — never
        an empty-deny;
      * when the person has positions at the active store we resolve their
        positions' catalog perms (per position: the SAVED config, else that
        position-role's catalog default) and apply the SUBTRACTIVE-within-
        MANAGED model (perms-rework Piece A): route tags bound in
        CATALOG_TO_TAGS (the MANAGED_TAGS set) become catalog-authoritative —
        an OFF toggle on a bound key now REVOKES the tag — while every UNbound
        route tag still passes through from the role baseline, so a positioned
        manager keeps all non-managed route access. The route
        @requires_permission tags are a DIFFERENT vocabulary from the catalog
        keys; CATALOG_TO_TAGS is the only bridge (see below). This is reached
        ONLY for a positioned user at their active store; the belt branches
        below stay on the full role baseline (lockout-safe).

    Cached per-request on flask.g keyed by user id (the decorator + the
    Jinja helper both call through here on the same request). Manages its
    own SessionLocal in try/finally and never raises out: on any error it
    falls back to role-perms (fail toward the safe role baseline, not an
    empty deny that would lock a legitimate user out)."""
    role = _canonical_role(getattr(user, "permission_level", None))
    role_perms = ROLE_PERMISSIONS.get(role, set())

    # Per-request cache (flask.g). Keyed by user id so impersonation /
    # multiple users in one request can't collide.
    uid = getattr(user, "id", None)
    try:
        cache = g._effective_perms_cache
    except Exception:
        cache = None
    if cache is None:
        cache = {}
        try:
            g._effective_perms_cache = cache
        except Exception:
            cache = None  # no app/request context — skip caching
    if cache is not None and uid in cache:
        return cache[uid]

    result = set(role_perms)  # default = the safe role baseline (holes 1 + 3)
    try:
        store = session.get("active_store")
        # Hole 1 + 3 belt: no active store -> role-perms.
        if store:
            from app.db import SessionLocal
            from app.models import (Employee, EmployeePosition, Position)
            from app.services.permission_catalog import (
                default_role_map, position_role)
            db = SessionLocal()
            try:
                emp = (db.query(Employee)
                       .filter(Employee.user_id == uid)
                       .first()) if uid is not None else None
                # Hole 1 belt: no linked Employee -> role-perms.
                if emp is not None:
                    # The person's position role-keys AT the active store.
                    pos_q = (db.query(Position.name)
                             .join(EmployeePosition,
                                   EmployeePosition.position_id == Position.id)
                             .filter(EmployeePosition.employee_id == emp.id))
                    # Sam #2606 "both stores": when the person picked BOTH, union the
                    # positions across ALL their stores (this only ADDS positions ->
                    # can only grant more, never lock out). Else scope to the one store.
                    if store != "__both__":
                        pos_q = pos_q.filter(EmployeePosition.store_key == store)
                    rows = pos_q.all()
                    pos_keys = set()
                    for (pname,) in rows:
                        pk = position_role(pname)
                        if pk:
                            pos_keys.add(pk)
                    # Hole 1 belt: no positions at the active store ->
                    # role-perms (the person isn't working that store yet).
                    if pos_keys:
                        drm = default_role_map()
                        union = set()
                        for pk in pos_keys:
                            saved = effective_perms_for([pk], store, db)
                            # Per-position: the SAVED config if any row
                            # exists for it, else that role's catalog
                            # default (a brand-new position with nothing
                            # toggled still gets its sensible baseline).
                            if saved:
                                union |= saved
                            else:
                                union |= drm.get(pk, set())
                        # Subtractive-within-MANAGED (perms-rework Piece A): the
                        # catalog is now AUTHORITATIVE for the route tags it is
                        # explicitly bound to (CATALOG_TO_TAGS / MANAGED_TAGS) — an
                        # OFF toggle on a bound catalog key now REVOKES the route
                        # tag, closing the additive-only gap. Every UNbound route
                        # tag still passes through from the role baseline (no
                        # lockout). Breakdown:
                        #   (role_perms - MANAGED_TAGS) = the role tags pass
                        #     through EXCEPT the managed ones (those are now
                        #     catalog-authoritative, so the role no longer grants
                        #     them unilaterally);
                        #   granted_catalog_tags = the managed route tags the
                        #     catalog GRANTS for this user's positions (a bound key
                        #     ON re-adds its tag; OFF omits it = REVOKED);
                        #   | union keeps the catalog keys themselves in the set
                        #     for compat — existing checks/templates AND the
                        #     enforce probe read catalog keys (dash.*, reports.*,
                        #     emp.* ...) directly out of this set.
                        # The route @requires_permission tags (ROLE_PERMISSIONS)
                        # and the catalog keys (permission_catalog.py) remain two
                        # DIFFERENT vocabularies; CATALOG_TO_TAGS is the only wire
                        # between them.
                        granted_catalog_tags = set()
                        for _ck in union:
                            granted_catalog_tags |= CATALOG_TO_TAGS.get(_ck, set())
                        result = (set(role_perms) - MANAGED_TAGS) | granted_catalog_tags | union
            finally:
                db.close()
    except Exception:
        # Fail toward the role baseline, never an empty deny.
        log.exception("effective-perms resolve failed; falling back to role-perms")
        result = set(role_perms)

    if cache is not None and uid is not None:
        cache[uid] = result
    return result


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
    # Hole 4: the partner WILDCARD is checked FIRST, off User.permission_level,
    # before any position logic — a partner can never self-lock (and the check
    # never touches the DB / active-store join). Stays on the static
    # ROLE_PERMISSIONS dict, the never-stubbed source of the wildcard.
    if "*" in ROLE_PERMISSIONS.get(role, set()):
        return True
    # perms-rework repoint: enforcement now reads the POSITION-based effective
    # set (saved PositionPermission union at the active store), gated BEHIND the
    # role-perms fallback inside _effective_perms — never an empty-deny.
    perms = _effective_perms(user)
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
