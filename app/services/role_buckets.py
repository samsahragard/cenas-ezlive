"""Per-store SECTION classifier for the Team-roster page (ck, S2).

The ONE missing concept the roster page needs: given a role (or scheduling
position), which per-store SECTION does it belong to -- MANAGEMENT, HOURLY,
or DRIVER? This is deliberately a SEPARATE concern from:

  - permission_catalog.py  -- "who can do what" (the permission grant catalog)
  - role_hierarchy.py      -- "who out-ranks whom / kitchen vs foh" (tier+domain)

...and it EXTENDS the existing permission_catalog (Sam's confirmed decision):
the role keys here are exactly permission_catalog.ROLES keys -- no competing
catalog is introduced.

SECTION model (Sam's confirmed decisions):
  - Corporate and Partner are a TOP TIER with both-stores access (store_scope
    NULL). They are NOT per-store section rows -- so they map to None here
    (section "above" the per-store sections), intentionally absent from
    SECTION_FOR_ROLE.
  - Three per-store sections matter on this page: MANAGEMENT, HOURLY, DRIVER
    (DRIVER is shown elsewhere / ez-driver, not on this page, but is still a
    real section so it is classified here).

Section role buckets (defaults Sam approved), reconciled to the EXACT role
keys found in permission_catalog.ROLES:
  MANAGEMENT = corporate_chef, gm, foh_manager, km, assistant_km, expo
  HOURLY     = bartender, busser, cashier, cook, server, well, host
  DRIVER     = corporate_driver   (the in-house Driver role; the LOCKED
               ezCater 'driver' is separate and not a catalog role)
  partner, corporate  => NOT section-placed => None

Role-key reconciliation notes:
  - Sam's bucket text said "gm (general manager)" / "general_manager vs gm":
    permission_catalog uses the key "gm" (label "General Manager"). We use
    "gm". No general_manager key exists.
  - "km" (label "Kitchen Manager"), "foh_manager", "assistant_km" all match
    the catalog keys verbatim.
  - "Well" -> the catalog role key is "well" (HOURLY). role_hierarchy maps the
    *position* 'Well' onto the bartender ROLE for FOH/BOH domain purposes, but
    permission_catalog keeps "well" as its own role key, so we classify the
    "well" role directly into HOURLY.
  - "Hostess" position -> role key "host" (HOURLY).
  - The catalog's Driver role key is "corporate_driver" (label "Driver").

KEEP IMPORTS LIGHT: only the pure dict/function we need from
permission_catalog are imported (ROLES list + position_role()). Those touch no
DB / Flask / models at import, so this module -- and its unit test -- import
without a database.
"""
from __future__ import annotations

from app.services.permission_catalog import ROLES, position_role

# ---- The three per-store sections (canonical lowercase tokens) ----
SECTION_MANAGEMENT = "management"
SECTION_HOURLY = "hourly"
SECTION_DRIVER = "driver"

# Ordered set of the real per-store sections. partner/corporate are NOT here
# (they are the tier-above, section=None).
SECTIONS = (SECTION_MANAGEMENT, SECTION_HOURLY, SECTION_DRIVER)


# ---- SECTION_FOR_ROLE -- the classifier table ----
# Every permission_catalog role key EXCEPT partner/corporate (intentionally
# absent -> section_for_role returns None for them). Keys verified against
# permission_catalog.ROLES.
SECTION_FOR_ROLE: dict[str, str] = {
    # MANAGEMENT (per-store managers + expo)
    "corporate_chef": SECTION_MANAGEMENT,
    "gm":             SECTION_MANAGEMENT,
    "km":             SECTION_MANAGEMENT,
    "assistant_km":   SECTION_MANAGEMENT,
    "foh_manager":    SECTION_MANAGEMENT,
    "expo":           SECTION_MANAGEMENT,
    # HOURLY (floor staff, both houses)
    "bartender":      SECTION_HOURLY,
    "busser":         SECTION_HOURLY,
    "cashier":        SECTION_HOURLY,
    "cook":           SECTION_HOURLY,
    "server":         SECTION_HOURLY,
    "well":           SECTION_HOURLY,
    "host":           SECTION_HOURLY,
    # DRIVER (in-house corporate driver; shown elsewhere / ez-driver)
    "corporate_driver": SECTION_DRIVER,
    # partner, corporate => intentionally ABSENT (tier-above -> None)
}


def section_for_role(role_key):
    """The per-store SECTION for a permission_catalog role key.

    Returns 'management' | 'hourly' | 'driver', or None for the tier-above
    roles (partner / corporate) and for any unknown / None key. Case- and
    whitespace-tolerant on the input. Never raises.
    """
    return SECTION_FOR_ROLE.get((role_key or "").strip().lower())


def section_for_position(position_name):
    """The per-store SECTION for a canonical scheduling-position NAME.

    Resolves the position name to its permission_catalog role via
    permission_catalog.position_role(), then classifies that role. Unknown /
    None position, or a position whose role is tier-above (partner/corporate)
    -> None. Never raises.
    """
    return section_for_role(position_role(position_name))


# ---- Internal consistency guard (cheap; runs at import) ----
# Guarantees SECTION_FOR_ROLE only ever maps to a real SECTION and that the
# tier-above roles stay intentionally absent. Catches a future typo (e.g. a
# mis-spelled section token, or partner/corporate accidentally added).
_KNOWN_ROLE_KEYS = {r["key"] for r in ROLES}
_TIER_ABOVE_ROLES = frozenset({"partner", "corporate"})

assert set(SECTION_FOR_ROLE.values()) <= set(SECTIONS), (
    "SECTION_FOR_ROLE maps to an unknown section token"
)
assert _TIER_ABOVE_ROLES.isdisjoint(SECTION_FOR_ROLE), (
    "partner/corporate must stay section-less (tier-above)"
)
# Every classified key must be a real catalog role (no orphan keys).
assert set(SECTION_FOR_ROLE).issubset(_KNOWN_ROLE_KEYS), (
    "SECTION_FOR_ROLE contains a key not in permission_catalog.ROLES"
)
