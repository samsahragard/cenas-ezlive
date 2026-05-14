"""Pay masking — Phase 2 / Block 1H (samai spec, revised).

The masking ALREADY EXISTS: app/services/toast_reports.py's
labor_report(...) redacts management pay via
role_classifier.is_management_position() — it implements Sam's
2026-05-09 redaction rule. 1H does NOT reinvent that.

1H is a thin CANONICAL ADAPTER: render_labor_breakdown(store, user) —
the ONE place a User is mapped to the redact_management flag, so every
Phase 2 surface that shows labor data (the ribbon's `sales` category
in 1C, the team tab in 1G, the Block 3 Q&A agent) masks consistently
instead of each surface re-deriving "should this viewer see
management pay."

The one genuinely-new piece of logic: _redact_management_for(user) —
partner sees everything (redact_management=False), every other role
(corporate included) sees management rows as pct_net_sales only
(redact_management=True). Everything else is plumbing over the
existing labor_report.

Open questions (spec §6) — implemented against samai's leans /
spec defaults; Sam's answers are refinements, not blockers:
  - Q1: path (b) — keep labor_report's per-title-redacted output
    as-is, no collapse into two named rollup lines. Zero new code,
    strictly more granular, still fully redacted.
  - Q2: keep labor_report's existing behavior — pct_net_sales
    survives redaction (changing it would mean editing labor_report
    itself, a wider blast radius).
  - Q3: default date window when start/end omitted = current
    week-to-date (Monday 00:00 → now).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.services.permissions import _canonical_role
from app.services.role_hierarchy import (
    _STORE_UNSCOPED_ROLES,
    _store_scopes_intersect,
)

log = logging.getLogger(__name__)


# ============================================================
# user → redact_management — the one new piece of logic (§4)
# ============================================================
def _is_partner(user) -> bool:
    """True iff `user`'s role resolves (through _LEGACY_ALIASES) to
    'partner'. Neither current legacy alias maps to partner, but
    resolving keeps this correct if the alias table grows."""
    if user is None:
        return False
    return _canonical_role(getattr(user, "permission_level", None)) == "partner"


def _redact_management_for(user) -> bool:
    """The §4 derivation: partner → False (sees every management row in
    full detail), every other role → True (management rows show only
    pct_net_sales). This is the whole 'new logic' of 1H."""
    return not _is_partner(user)


# ============================================================
# Default date window (§6 Q3) — current week-to-date
# ============================================================
def _default_window() -> tuple[datetime, datetime]:
    """Monday 00:00 of the current week → now. Used when a caller (the
    ribbon) doesn't pass start/end."""
    now = datetime.utcnow()
    monday = now - timedelta(days=now.weekday())
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, now


# ============================================================
# Defense-in-depth store-scope guard (§5)
# ============================================================
def _store_scope_refused(store: str | None, user) -> bool:
    """True if `user` is store-scoped (gm and below) and `store` is not
    in their scope — render_labor_breakdown then returns a refused
    result rather than calling labor_report at all.

    partner / corporate / corporate_chef / prep_manager are
    store-unscoped — the guard is a no-op for them. This is the inner
    belt of a belt-and-suspenders pair; the caller (1G's team tab)
    still gates store access before calling 1H."""
    role = _canonical_role(getattr(user, "permission_level", None)) \
        if user is not None else None
    if role in _STORE_UNSCOPED_ROLES:
        return False  # store-unscoped — guard is a no-op
    if not store:
        # No specific store requested — nothing to guard against
        # (an all-stores request from a store-scoped user is still
        # bounded by labor_report's own location handling; we don't
        # refuse it here).
        return False
    user_store = getattr(user, "store_scope", None)
    return not _store_scopes_intersect(user_store, store)


def _refused_result(store: str | None) -> dict:
    """The shape returned when the store-scope guard refuses — mirrors
    labor_report's dict shape (empty) so callers can treat it
    uniformly, plus a refused:True flag."""
    return {
        "refused": True,
        "store": store,
        "rows": [],
        "total_cost": None,
        "overall_pct": None,
        "total_hours": None,
        "total_shifts": None,
    }


# ============================================================
# render_labor_breakdown — the canonical adapter (§3)
# ============================================================
def render_labor_breakdown(store, user, start=None, end=None) -> dict:
    """Canonical entry point for labor data anywhere in Phase 2.

    Derives redact_management from `user` (§4), runs the
    defense-in-depth store-scope guard (§5), applies the default
    week-to-date window if start/end omitted (§6 Q3), then returns
    labor_report(...)'s dict. Per §6 Q1/Q2 (samai's leans): no
    two-line collapse, pct_net_sales survives redaction — both are
    labor_report's existing behavior, so this is a thin pass-through.

    Defensive: if labor_report raises (Toast API down, etc.) this
    returns an error-flagged dict rather than propagating — 1C's
    sales adapter + 1G's team tab both call this, and a Toast outage
    must not 500 the surfaces that embed it. (1C's ribbon path is
    also wrapped by ck's ribbon_render_context, but defense-in-depth.)
    """
    # §5 — store-scope guard, before any data call.
    if _store_scope_refused(store, user):
        log.info(
            "render_labor_breakdown refused: store=%r out of scope for "
            "user_id=%r role=%r",
            store, getattr(user, "id", None),
            getattr(user, "permission_level", None))
        return _refused_result(store)

    # §4 — the one new piece of logic.
    redact = _redact_management_for(user)

    # §6 Q3 — default window.
    if start is None or end is None:
        start, end = _default_window()

    # store → labor_report's location_filter. labor_report treats
    # location_filter in ("tomball", "copperfield") as a single-store
    # filter; anything else (None, "both") → all stores.
    location_filter = store if store in ("tomball", "copperfield") else None

    try:
        from app.services.toast_reports import labor_report
        report = labor_report(
            start, end,
            location_filter=location_filter,
            redact_management=redact,
        )
        # Tag the result so callers can see what masking was applied.
        if isinstance(report, dict):
            report.setdefault("refused", False)
            report["redact_management"] = redact
        return report
    except Exception as e:  # noqa: BLE001 — must not 500 the embedding surface
        log.exception(
            "render_labor_breakdown: labor_report failed (store=%r "
            "user_id=%r): %s", store, getattr(user, "id", None), e)
        return {
            "refused": False,
            "error": type(e).__name__,
            "store": store,
            "rows": [],
            "total_cost": None,
            "overall_pct": None,
            "redact_management": redact,
        }
