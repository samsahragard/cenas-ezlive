"""Org-tier / domain structure for the role taxonomy.

Phase 2 / Block 1 precondition (samai spec, Sam §5.3 Path A 2026-05-14).
This module is the org-hierarchy concern — "who out-ranks whom, who works
which side of the house" — kept deliberately separate from
app/services/permissions.py's tag-grant concern ("who can do what"). They
answer different questions and evolve independently.

Three things live here:
  - ROLE_TIER     — every role → an integer authority tier (higher = more)
  - role_tier()   — resolve a User.permission_level to its tier
  - role_domain() — resolve a role to "kitchen" | "foh" | "both"

First consumers: 1A's can_assign_to (needs both functions), 2C's
can_view_counseling cascade (needs role_tier). 1E's escalation will need
an immediate_manager(user, db) helper — that is deliberately NOT here
yet: it needs a DB session, this module is pure, and no current consumer
needs it. Whichever sub-block first needs it adds it.

Pure module: no DB, no Flask. Import-safe from anywhere.
"""
from __future__ import annotations

# Single source of truth for legacy User.permission_level values — do not
# re-declare, import from permissions.py.
from app.services.permissions import _LEGACY_ALIASES


# ---- ROLE_TIER — every role, every tier ----
# Tiers from the Phase 2 directive's §2C hierarchy line:
#   partner > corporate > gm > km/asst_km/manager/foh_manager > hourly
# corporate_chef at tier 4 is the 1A-spec §11 Q3
# provisional placements (defensible defaults; Sam to confirm — precond
# spec §6 Q2). The five hourly roles (cook/server/busser/host/bartender)
# are the §5.3 Path A additions; everything else is an existing canonical
# ROLE_PERMISSIONS key.
ROLE_TIER: dict[str, int] = {
    # tier 5 — partner
    "partner":        5,
    # tier 4 — corporate (corporate + the multi-store corporate_chef)
    "corporate":      4,
    "corporate_chef": 4,
    # tier 3 — gm
    "gm":             3,
    # tier 2 — manager tier
    "km":             2,
    "assistant_km":   2,
    "foh_manager":    2,
    # tier 1 — hourly tier
    "expo":           1,
    "driver":         1,
    "cook":           1,   # NEW — §5.3 Path A
    "server":         1,   # NEW
    "busser":         1,   # NEW
    "host":           1,   # NEW
    "bartender":      1,   # NEW
    # Schedules V2 (Sam #1742): floor cashier + the employee scheduling
    # identity + the in-house corporate_driver - bottom working tier.
    "cashier":        1,
    "employee":       1,
    "corporate_driver": 1,
}


# ---- _ROLE_DOMAIN — kitchen / foh / both ----
# The kitchen/foh split is the cross-cut can_assign_to needs below the GM
# tier (a KM assigns to kitchen staff; an FOH manager to FOH staff). The
# hourly assignments align with role_classifier.py's BOH/FOH rule (Sam's
# 2026-05-09 rule: cook = BOH/kitchen; bussers, expo, servers, hosts =
# FOH) — so role_domain here and role_classifier.classify_role agree on
# the same staff, just keyed differently (app role vs Toast job title).
# partner / corporate / gm cross domains → "both".
_ROLE_DOMAIN: dict[str, str] = {
    "partner":        "both",
    "corporate":      "both",
    "gm":             "both",
    "corporate_chef": "kitchen",
    "km":             "kitchen",
    "assistant_km":   "kitchen",
    "foh_manager":    "foh",
    "cook":           "kitchen",   # NEW — §5.3 Path A
    "server":         "foh",       # NEW
    "busser":         "foh",       # NEW
    "host":           "foh",       # NEW
    "bartender":      "foh",       # NEW
    "expo":           "foh",
    "driver":         "foh",
    "cashier":        "foh",
    "employee":       "foh",
    "corporate_driver": "foh",
}


def _resolve(role: str | None) -> str | None:
    """Normalize a User.permission_level value: pass canonical roles
    through unchanged, translate legacy aliases ('manager' → 'gm',
    'corporate-driver' → 'driver'). None / unknown → None."""
    if not role:
        return None
    if role in ROLE_TIER:
        return role
    return _LEGACY_ALIASES.get(role)


def role_tier(role: str | None) -> int:
    """Authority tier for a User.permission_level value. Resolves
    _LEGACY_ALIASES first ('manager' → gm → 3, 'corporate-driver' →
    driver → 1). Unknown / None → 0 (no authority — an unknown role can
    never out-rank anyone, so can_assign_to and can_view_counseling both
    fail safe-closed for it). Never raises."""
    resolved = _resolve(role)
    if resolved is None:
        return 0
    return ROLE_TIER.get(resolved, 0)


def role_domain(role: str | None) -> str:
    """'kitchen' | 'foh' | 'both' for a role. Resolves legacy aliases.
    Unknown / None → 'foh' — the safe default (matches role_classifier's
    'everything else is front' rule; an unknown role can never be
    mis-assigned kitchen-only work it shouldn't see). Never raises."""
    resolved = _resolve(role)
    if resolved is None:
        return "foh"
    return _ROLE_DOMAIN.get(resolved, "foh")


# ---- can_assign_to — the task-assignment helper (Block 1A §5) ----

# Store-unscoped roles skip the store-constraint check in can_assign_to:
# they have authority across both stores. Everyone else (gm and below)
# is store-scoped and may only assign to a target in their store.
_STORE_UNSCOPED_ROLES = frozenset({
    "partner", "corporate", "corporate_chef",
})

# A store_scope value → the set of physical stores it covers. None /
# unknown → empty set (covers nothing), so the intersection check fails
# safe-closed. "none" is a Task.store_scope value, not a User one, but
# handled here for completeness.
_STORE_SETS: dict[str, frozenset[str]] = {
    "tomball":     frozenset({"tomball"}),
    "copperfield": frozenset({"copperfield"}),
    "both":        frozenset({"tomball", "copperfield"}),
    "none":        frozenset(),
}


def _store_scopes_intersect(a: str | None, b: str | None) -> bool:
    """True if two store_scope values share at least one physical store.
    None / unknown → empty set → no intersection (safe-closed)."""
    sa = _STORE_SETS.get((a or "").strip().lower(), frozenset())
    sb = _STORE_SETS.get((b or "").strip().lower(), frozenset())
    return bool(sa & sb)


def can_assign_to(actor, target) -> bool:
    """True if ``actor`` may assign / reassign a task to ``target``.

    Block 1A spec §5.2 assignment rules:
      - any role → themselves (actor.id == target.id) — always allowed
      - partner → anyone strictly below
      - corporate → gm and every tier below (store-unscoped)
      - gm → manager-tier + hourly-tier, target in the GM's store
      - km / assistant_km → hourly-tier KITCHEN staff, target in scope
      - foh_manager → hourly-tier FOH staff, target in scope
      - never to a same-tier peer — strictly downward only, except self

    ``actor`` / ``target`` are User rows; this reads only ``.id``,
    ``.permission_level`` and ``.store_scope`` via attribute access, so
    the function stays pure (no DB, no Flask) — consistent with the rest
    of this module.
    """
    # 1. Self-assignment — every role, including hourly tier, always.
    if getattr(actor, "id", None) is not None and actor.id == getattr(target, "id", None):
        return True

    actor_role = getattr(actor, "permission_level", None)
    target_role = getattr(target, "permission_level", None)
    a_tier = role_tier(actor_role)
    t_tier = role_tier(target_role)

    # 2. Unknown actor role → tier 0 → no authority to assign anyone.
    if a_tier == 0:
        return False

    # 3. Strictly downward only — never to a same-tier peer (gm→gm,
    #    corporate→corporate) or upward. Self already handled above.
    if a_tier <= t_tier:
        return False

    # 4. Manager-tier actors (km / assistant_km / foh_manager — tier 2)
    #    assign ONLY down to the hourly tier, and only within their own
    #    domain (a KM assigns kitchen staff; an FOH manager assigns FOH
    #    staff). gm (tier 3) and above cross domains, no restriction.
    if a_tier == 2:
        if t_tier != 1:
            return False
        if role_domain(actor_role) != role_domain(target_role):
            return False

    # 5. Store constraint — store-scoped actors (everyone except the
    #    store-unscoped partner / corporate / corporate_chef) may only assign to a target whose store scope
    #    intersects their own.
    if _resolve(actor_role) not in _STORE_UNSCOPED_ROLES:
        if not _store_scopes_intersect(
                getattr(actor, "store_scope", None),
                getattr(target, "store_scope", None)):
            return False

    return True


# ---- immediate_manager — 1E's escalation lookup (Block 1 precond §1) ----
# Deferred from the role-taxonomy precondition ("1E's escalation is its
# first real consumer, it needs a db session") — 1E adds it here, the
# shared home for the hierarchy concern. Unlike the rest of this module
# it takes a db session; it stays import-safe (User is imported lazily,
# inside the function, so module load never touches app.models).
def immediate_manager(user, db):
    """The lowest-tier active user STRICTLY ABOVE ``user``'s tier whose
    authority covers ``user``'s store — i.e. who a missed task escalates
    to. Returns a User row, or None if nobody qualifies (e.g. a partner
    has no manager).

    Contract DEFINED HERE, not inherited: the Block 1 precondition spec
    deferred immediate_manager with no contract ("1E's escalation will
    need an immediate_manager(user, db) helper ... whichever sub-block
    first needs it adds it"). 1E is that first consumer, so 1E defines
    the contract. samai's 1E review blessed this definition and owns a
    precondition-spec §2.4 amendment documenting it, so spec and code
    stay coherent. Concretely:
      - candidate.tier must be strictly > user.tier
      - candidate must be active
      - candidate's authority must cover user's store: a store-unscoped
        candidate (partner / corporate / corporate_chef)
        covers everyone; a store-scoped candidate's store_scope must
        intersect user's store_scope
      - among the qualifying candidates, the LOWEST tier wins (closest
        manager). Within that lowest tier, a same-domain candidate is
        preferred (a kitchen cook escalates to a kitchen manager, not an
        FOH manager). This domain preference is a tie-break WITHIN the
        lowest tier — it never promotes a higher tier — and exists
        because cross-domain escalation reads operationally wrong;
        samai's 1E review explicitly blessed it.
      - final tie-break: lowest User.id, for determinism.

    db is an active Session — caller owns its lifecycle.
    """
    from app.models import User  # lazy — keeps module load import-safe

    user_role = getattr(user, "permission_level", None)
    u_tier = role_tier(user_role)
    if u_tier >= 5:
        return None  # partner — nobody strictly above

    u_store = getattr(user, "store_scope", None)
    u_domain = role_domain(user_role)

    candidates = []
    for cand in db.query(User).filter(User.active.is_(True)).all():
        if cand.id == getattr(user, "id", None):
            continue
        c_tier = role_tier(cand.permission_level)
        if c_tier <= u_tier:
            continue  # not strictly above
        # store-coverage check
        if _resolve(cand.permission_level) in _STORE_UNSCOPED_ROLES:
            covers = True
        else:
            covers = _store_scopes_intersect(cand.store_scope, u_store)
        if not covers:
            continue
        candidates.append(cand)

    if not candidates:
        return None

    lowest_tier = min(role_tier(c.permission_level) for c in candidates)
    closest = [c for c in candidates
               if role_tier(c.permission_level) == lowest_tier]
    # domain-preference refinement, then id tie-break
    same_domain = [c for c in closest
                   if role_domain(c.permission_level) == u_domain]
    pool = same_domain or closest
    return min(pool, key=lambda c: c.id)
