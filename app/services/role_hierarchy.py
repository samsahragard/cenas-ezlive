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
# corporate_chef at tier 4 + prep_manager at tier 3 are the 1A-spec §11 Q3
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
    # tier 3 — gm + the multi-store prep_manager
    "gm":             3,
    "prep_manager":   3,
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
    "prep_manager":   "kitchen",
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
