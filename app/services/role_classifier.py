"""BOH (Back of House) vs FOH (Front of House) position classifier.

Sam's rule (2026-05-09): "Cook, Prep, Grill, Dish, Enchilada = BOH;
everything else (incl. Bussers, Expo) = FOH."

This is shared between Sling (54 positions, used for Roster + Schedule)
and Toast (~28 distinct job titles, used for Labor cost breakdown).
"""
from __future__ import annotations

# Explicit BOH list. Lowercase, exact-match (with substring fallback below).
# These are positions where the person is physically at a kitchen station —
# cooking, prepping, washing dishes, on the grill, plating enchiladas.
_BOH_EXACT = {
    "cook",          "c-cook",
    "prep",          "c-prep",          "prep meat",
    "grill",         "c-grill",
    "dish",          "c-dishwasher",    "dishwasher",
    "enchilada",     "c-enchilada",
    "chop",
    "chips",
    "window",
    "well",          # kitchen well station (steam table). NOTE: "bar well" is FOH.
    "kitchen manager",
    "asst kitchen manager",
    "asst. kitchen manager",
    "assistant kitchen manager",
}

# Position-name substrings that are BOH if they appear anywhere in the name —
# catches future variants we haven't seen yet (e.g., "Lead Cook", "Prep Lead").
_BOH_SUBSTRINGS = ("cook", "prep", "grill", "dishwasher", "enchilada", "kitchen")

# Explicit FOH overrides — these contain BOH-ish substrings but are bar/service.
_FOH_OVERRIDES = {"bar well", "bar back"}


def classify_role(position_name: str | None) -> str:
    """Return 'boh' or 'foh' for a position name.

    Defaults to 'foh' for unknown / empty inputs (Sam's "everything else is front" rule)."""
    if not position_name:
        return "foh"
    name = position_name.strip().lower()
    if name in _FOH_OVERRIDES:
        return "foh"
    if name in _BOH_EXACT:
        return "boh"
    # Substring fallback for unknown variants. The FOH overrides above prevent
    # "bar well" from matching the "well" rule.
    for kw in _BOH_SUBSTRINGS:
        if kw in name:
            return "boh"
    return "foh"


def is_boh(position_name: str | None) -> bool:
    return classify_role(position_name) == "boh"


def is_foh(position_name: str | None) -> bool:
    return classify_role(position_name) == "foh"
