"""Universal ribbon — Phase 2 / Block 1, sub-blocks 1B + 1C.

1B (ck) ships this module with RIBBON_CATEGORIES + a stub
ribbon_items_for() that returns []. 1C (aick) replaces the stub body
with the real content router: it pulls from Task / Signal /
SalesInsight / ScheduledEvent, filters by role + store_scope +
page_slug relevance + per-user dismissals, and returns RibbonItem
render-contract objects sorted severity DESC then deadline ASC.

THE RENDER CONTRACT (Block 1B spec §2) — the interface between 1B's
_ribbon.html partial (consumer) and 1C's router (producer):

    class RibbonItem:
        category:    str    # one of the RIBBON_CATEGORIES slugs below
        severity:    str    # "info" | "warn" | "alert"
        item_type:   str    # "task" | "signal" | "sales_insight"
                            #   (1C's spec proposes a 4th: "scheduled_event"
                            #    — 1B's partial is item_type-agnostic so it
                            #    passes any value straight through; the
                            #    enumeration matters for 1D's handler + 1C's
                            #    producer, not for 1B's markup)
        item_id:     int    # the underlying row id in the item_type table
        deadline_at: datetime | None
        can_dismiss: bool   # whether the X control renders
        can_check:   bool   # whether the Check control renders

        def render_for(self, user) -> dict:
            # returns {"text": str, "sub_text": str | None,
            #          "styling_class": str}

1B's partial only ever touches those seven attributes + render_for();
1C implements the class. Any change to this contract is a coordinated
1B+1C change and gets re-specced.
"""
from __future__ import annotations


# Fixed vertical order — the partial renders all seven categories
# every time, empty ones included (predictable structure + stable
# collapse toggles). Slugs are mostly plural; the Task.category →
# ribbon-slug mapping is 1C's content-router concern. 1B only needs
# these seven slugs + their display labels.
RIBBON_CATEGORIES = [
    ("todo",        "To-do"),
    ("caterings",   "Caterings"),
    ("events",      "Events"),
    ("employee",    "Employee"),
    ("vendors",     "Vendors"),
    ("maintenance", "Maintenance"),
    ("sales",       "Sales"),
]

# Just the slugs, for fast membership checks — the collapse-toggle
# endpoint validates its <category> path arg against this set.
RIBBON_CATEGORY_SLUGS = frozenset(slug for slug, _label in RIBBON_CATEGORIES)


def ribbon_items_for(page_slug, user, store_scope, category=None):
    """STUB (Block 1B). Returns [] so _ribbon.html renders an
    all-empty ribbon without erroring during the 1B/1C parallel-build
    window. 1C replaces this body with the real content router.

    The signature is the agreed 1B↔1C contract — 1C must keep it:

        page_slug   — the current page's slug (the template `active`
                      var; None on pages that don't set one).
        user        — g.current_user (may be None pre-keypad-auth;
                      1C's router must tolerate that).
        store_scope — the user's store scope (the template
                      `store_slug` var: dos / uno / corporate /
                      partner).
        category    — optional. If a slug, 1C filters to that one
                      category. 1B's partial calls with category=None
                      (one call, groups in-template — fewer
                      round-trips; spec §4 default).

    Returns: list[RibbonItem] (see the module docstring for the
    render contract), already sorted severity DESC then deadline ASC.
    The stub returns an empty list.
    """
    return []
