"""Universal ribbon — Phase 2 / Block 1, sub-blocks 1B + 1C.

1B (ck) shipped this module with RIBBON_CATEGORIES + a stub
ribbon_items_for() returning []. 1C (aick) replaces the stub with the
real content router: it pulls from Task / Signal / SalesInsight /
ScheduledEvent, filters by role + store-scope + page-relevance +
per-user dismissals, and returns RibbonItem render-contract objects
sorted severity DESC then deadline ASC.

THE RENDER CONTRACT (Block 1B spec §2) — the interface between 1B's
_ribbon.html partial (consumer) and 1C's router (producer):

    class RibbonItem:
        category:    str    # one of the RIBBON_CATEGORIES slugs below
        severity:    str    # "info" | "warn" | "alert"
        item_type:   str    # "task" | "signal" | "sales_insight"
                            #   | "scheduled_event" | "ambient_signal"
                            #   (1C added scheduled_event as the 4th, 1J
                            #     adds ambient_signal as the 5th — each a
                            #     coordinated contract change; 1B's partial
                            #     is item_type-agnostic so it passes any
                            #     value straight through)
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

1C-internal additions (not part of the 1B contract): `relation` drives
render_for's framing; `_source` is the underlying row; `_ctx` holds
adapter-pre-resolved bits (owner names etc.) so render_for needs no DB.

DEPENDENCY STATUS at build time (Sam's locked order is
1A→1C→1H→1E→1F→1I, so 1C precedes 1F + 1H):
  - Task / RibbonItemDismissal : 1A — landed + samai-PASSed. Task
    adapter + dismissal-exclusion are FULLY wired.
  - Signal                    : Phase 1 anomaly engine — exists.
    Signal adapter FULLY wired.
  - ScheduledEvent            : Block 1 precondition 47830e6 — exists.
    ScheduledEvent adapter FULLY wired.
  - SalesInsight              : 1F — NOT YET BUILT. The adapter
    function is written (pure, duck-typed) but the router's gather
    step is import-guarded: no SalesInsight model → that source
    contributes nothing. Un-stubs automatically when 1F lands.
  - render_labor_breakdown    : 1H — NOT YET BUILT. The sales-category
    pay-masking hook (_apply_pay_masking) is a pass-through stub with
    a TODO; wire to 1H's helper when it lands.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from app.db import SessionLocal
from app.models import (
    Task, TaskAuditLog, RibbonItemDismissal, Signal, User, AmbientSignal,
)
from app.services.role_hierarchy import (
    _STORE_UNSCOPED_ROLES,
    _store_scopes_intersect,
)

log = logging.getLogger(__name__)


# ============================================================
# Constants
# ============================================================

# Fixed vertical order — the partial renders all seven categories
# every time, empty ones included (predictable structure + stable
# collapse toggles). Slugs are mostly plural; the Task.category →
# ribbon-slug mapping is 1C's content-router concern (_TASK_CATEGORY_TO
# _RIBBON below). 1B only needs these seven slugs + their labels.
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

# Severity rank for the sort (§8) — alert sorts before warn before info.
_SEVERITY_RANK = {"alert": 3, "warn": 2, "info": 1}

# Task.category (singular, from the 1A model) → ribbon-category slug
# (mostly plural). "general" maps to None — a general task surfaces in
# the `todo` category only, never its own domain section (1C spec §6).
_TASK_CATEGORY_TO_RIBBON = {
    "vendor":      "vendors",
    "catering":    "caterings",
    "event":       "events",
    "employee":    "employee",
    "maintenance": "maintenance",
    "sales":       "sales",
    "general":     None,
}

# page_slug → the ribbon category that page's domain detail belongs to.
# 1C spec §7.3 (amendment #1394, 2026-05-14): the map is explicit and
# authoritative. A slug NOT in this map renders no ribbon at all (empty
# list — a safe blank, not a guess). The "dashboard" slug maps to None,
# the sentinel for "all categories, unfiltered" (the overview intent;
# the 1B partial hides empty sections per §10). aick + ck extend the
# map as new pages adopt the ribbon — there is no implicit fallback.
_PAGE_TO_DOMAIN = {
    "dashboard":    None,           # sentinel — all 7 categories, unfiltered
    "notifications": None,          # sentinel — same shape as dashboard
                                    # (cena #2767 + aick #2768: /partner/notifications
                                    # page IS the all-categories overview after
                                    # ribbon retire at da61280; missing this mapping
                                    # was the root cause of empty notifications page)
    "orders":       "vendors",      # placeholder — refine as pages adopt
    "vendors":      "vendors",
    "produce":      "vendors",
    "roster":       "employee",
    "team":         "employee",
    "maintenance":  "maintenance",
    "caterings":    "caterings",
    "events":       "events",
    "reports":      "sales",
    "ezmarket":     "sales",
}

# Signal → ribbon category. The real map, per samai's 1C spec §13.1
# addendum (Q3 RESOLVED) — confirmed against the anomaly_rules.py
# taxonomy: rule_names are `domain.specific` across ten domains.
# Keyed on the rule_name domain prefix, with full-rule_name overrides
# (_SIGNAL_RULENAME_OVERRIDES) checked FIRST where a prefix splits.
# Two open questions stay flagged for Sam in the spec (§13.1 Q4/Q5):
# customer.* → employee is a least-bad fit; system.* → maintenance is
# the v1 default but Sam may prefer excluding system.* from the
# operational ribbon entirely.
_SIGNAL_PREFIX_TO_RIBBON = {
    "vendor.":     "vendors",
    "produce.":    "vendors",
    "orders.":     "caterings",   # ezCater catering-order fulfillment
    "sales.":      "sales",
    "labor.":      "employee",
    "attendance.": "employee",
    "server.":     "employee",
    "customer.":   "employee",    # §13.1 Q4 — least-bad fit
    "kitchen.":    "employee",
    "system.":     "maintenance",  # §13.1 Q5 — v1 default
}
# Full-rule_name overrides — checked before the prefix map (a prefix
# splits, one specific rule routes differently).
_SIGNAL_RULENAME_OVERRIDES = {
    "kitchen.equipment_down": "maintenance",
}
_SIGNAL_DEFAULT_RIBBON = "employee"


# ============================================================
# RibbonItem — the render-contract object
# ============================================================
class RibbonItem:
    """One ribbon row. Implements the 1B §2 render contract exactly,
    plus 1C-internal fields (relation / _source / _ctx) that drive
    render_for's framing without the partial ever seeing them.

    One source row can yield TWO RibbonItems (1C spec §3): Andres's
    task on his Vendors page produces an `owner_todo` item in `todo`
    AND an `owner_category` item in `vendors` — same item_type +
    item_id, two framings. 1D's X/Check acts on the underlying row
    regardless of which rendered copy was clicked.
    """

    __slots__ = (
        "category", "severity", "item_type", "item_id", "deadline_at",
        "can_dismiss", "can_check", "relation", "_source", "_ctx",
    )

    def __init__(self, *, category, severity, item_type, item_id,
                 deadline_at, can_dismiss, can_check, relation,
                 source=None, ctx=None):
        self.category = category
        self.severity = severity
        self.item_type = item_type
        self.item_id = item_id
        self.deadline_at = deadline_at
        self.can_dismiss = can_dismiss
        self.can_check = can_check
        # 1C-internal
        self.relation = relation
        self._source = source
        self._ctx = ctx or {}

    # --- render_for: the cross-cutting framing rule (1C spec §5) ---
    def render_for(self, user) -> dict:
        """Return {text, sub_text, styling_class}. Branches on
        item_type first, then (for tasks) relation. Signals /
        SalesInsights / ScheduledEvents have no owner — always
        observer-style status text."""
        styling = f"ribbon-item--{self.severity}"
        if self.item_type == "task":
            return self._render_task(styling)
        if self.item_type == "signal":
            text = self._ctx.get("text", "")
            return {"text": text, "sub_text": self._ctx.get("sub_text"),
                    "styling_class": styling}
        if self.item_type == "sales_insight":
            return {"text": self._ctx.get("text", ""),
                    "sub_text": self._ctx.get("sub_text"),
                    "styling_class": styling}
        if self.item_type == "scheduled_event":
            return {"text": self._ctx.get("text", ""),
                    "sub_text": self._ctx.get("sub_text"),
                    "styling_class": styling}
        if self.item_type == "ambient_signal":
            # 1J data-plane signal — observer-style, like sales_insight /
            # scheduled_event; the adapter pre-resolves text/sub_text from
            # the AmbientSignal payload into _ctx.
            return {"text": self._ctx.get("text", ""),
                    "sub_text": self._ctx.get("sub_text"),
                    "styling_class": styling}
        # Unknown item_type — defensive, should never happen.
        return {"text": self._ctx.get("text", ""), "sub_text": None,
                "styling_class": styling}

    def _render_task(self, styling: str) -> dict:
        """The four-way Task framing (1C spec §5 table)."""
        title = self._ctx.get("title", "task")
        when = self._ctx.get("deadline_str", "")
        owner_name = self._ctx.get("owner_name", "someone")
        if self.relation == "owner_todo":
            # action-required — imperative, what to do + when
            txt = f"{title}" + (f" — due {when}" if when else "")
            return {"text": txt, "sub_text": None,
                    "styling_class": styling}
        if self.relation == "owner_category":
            # informational — you already see it actionably in To-do
            return {"text": title,
                    "sub_text": f"Owner: you{', due ' + when if when else ''}",
                    "styling_class": styling}
        if self.relation == "observer":
            # status — who owns it + what's pending
            sub = f"Owner: {owner_name}" + (f", due {when}" if when else "")
            return {"text": title, "sub_text": sub,
                    "styling_class": styling}
        if self.relation == "escalated_to":
            # escalation — late + landed on the viewer + original owner
            return {"text": f"ESCALATED: {title}",
                    "sub_text": (f"was due {when}, owner {owner_name}"
                                 if when else f"owner {owner_name}"),
                    "styling_class": styling + " ribbon-item--escalated"}
        # Unknown relation — defensive.
        return {"text": title, "sub_text": None, "styling_class": styling}


# ============================================================
# Severity + deadline helpers
# ============================================================
def _task_severity(task, now: datetime) -> str:
    """Derive a Task's ribbon severity (Tasks have no severity column).
    JUDGMENT CALL — the 1C spec §5/§6 don't define Task severity
    explicitly; this is the sensible derivation, flagged for samai:
      - escalated, or past deadline   → alert
      - deadline within the next 24h  → warn
      - otherwise                     → info
    """
    if task.escalated_to_user_id is not None:
        return "alert"
    if task.deadline_at is not None:
        if task.deadline_at < now:
            return "alert"
        if task.deadline_at < now + timedelta(hours=24):
            return "warn"
    return "info"


def _deadline_str(dt: datetime | None) -> str:
    """Short human deadline for render_for text, e.g. "5/14 3:00PM".
    Empty string if None. Formatted manually rather than via strftime
    — the no-leading-zero codes (%-m / %-I) are a glibc extension and
    raise ValueError on Windows (AiCk + CI run on Windows)."""
    if dt is None:
        return ""
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt.month}/{dt.day} {hour12}:{dt.minute:02d}{ampm}"


def _ribbon_sort_key(item: RibbonItem):
    """Sort key (1C spec §8): severity DESC, then deadline ASC, with
    deadline_at IS NULL last within its severity band. Returns a tuple
    that sorts ascending into the desired order:
      - -severity_rank   (alert=-3 sorts before info=-1)
      - has_no_deadline  (False=0 before True=1 → null deadlines last)
      - deadline_at or datetime.max
    """
    rank = _SEVERITY_RANK.get(item.severity, 0)
    no_deadline = item.deadline_at is None
    return (-rank, no_deadline, item.deadline_at or datetime.max)


# ============================================================
# Source adapters (1C spec §6)
# ============================================================
def _task_to_items(task, user, page_domain, owner_name) -> list[RibbonItem]:
    """A Task → one or two RibbonItems (1C spec §3 + §6).

    - owner / escalated-to viewer → a `todo`-category item
      (relation owner_todo or escalated_to)
    - AND/OR the task's domain category (vendor→vendors etc.) as a
      separate item — owner_category if the viewer owns it, observer
      if they don't. `general` tasks have no domain category.
    The 'manager's to-do shows nothing unless escalated' rule (§5) is
    enforced by only setting owner_todo when owner_user_id == user.id
    and escalated_to when escalated_to_user_id == user.id.
    """
    now = datetime.utcnow()
    severity = _task_severity(task, now)
    is_owner = task.owner_user_id == user.id
    is_escalated_to = task.escalated_to_user_id == user.id
    domain_cat = _TASK_CATEGORY_TO_RIBBON.get(task.category)
    ctx = {
        "title": task.title,
        "deadline_str": _deadline_str(task.deadline_at),
        "owner_name": owner_name,
    }

    def _mk(category, relation, can_check):
        return RibbonItem(
            category=category, severity=severity, item_type="task",
            item_id=task.id, deadline_at=task.deadline_at,
            can_dismiss=True, can_check=can_check, relation=relation,
            source=task, ctx=ctx,
        )

    items: list[RibbonItem] = []

    # --- todo-category item ---
    if is_escalated_to:
        # escalated to the viewer → shows in their todo, escalation
        # framing. can_check yes (they can complete it now it's theirs).
        items.append(_mk("todo", "escalated_to", can_check=True))
    elif is_owner:
        items.append(_mk("todo", "owner_todo", can_check=True))
    # else: a manager does NOT see a subordinate's task in their own
    # todo (the §5 rule) — only as an observer in the domain category.

    # --- domain-category item ---
    if domain_cat is not None:
        if is_owner:
            # owner sees it again as context in its domain category
            items.append(_mk(domain_cat, "owner_category", can_check=True))
        else:
            # observer — in scope on a domain page; can_check False
            # (an observer cannot complete someone else's task)
            items.append(_mk(domain_cat, "observer", can_check=False))

    return items


def _signal_to_item(signal, user) -> RibbonItem:
    """A Signal (anomaly engine) → one RibbonItem. Signals have no
    owner — always observer-style. category mapped from the signal's
    rule_name prefix (1C spec §13 Q3 — best-effort until samai specs
    the full anomaly-rule-category → ribbon-category map)."""
    rn = signal.rule_name or ""
    # Full-rule_name override first (a prefix splits — e.g.
    # kitchen.equipment_down → maintenance, not employee), then the
    # domain-prefix map, then the low-risk default.
    if rn in _SIGNAL_RULENAME_OVERRIDES:
        category = _SIGNAL_RULENAME_OVERRIDES[rn]
    else:
        category = _SIGNAL_DEFAULT_RIBBON
        for prefix, cat in _SIGNAL_PREFIX_TO_RIBBON.items():
            if rn.startswith(prefix):
                category = cat
                break
    severity = signal.severity if signal.severity in _SEVERITY_RANK else "info"
    ctx = {
        "text": signal.subject_label or signal.rule_name or "Signal",
        "sub_text": signal.action_text or None,
    }
    return RibbonItem(
        category=category, severity=severity, item_type="signal",
        item_id=signal.id, deadline_at=None,
        can_dismiss=True, can_check=True, relation="observer",
        source=signal, ctx=ctx,
    )


def _sales_insight_to_item(insight) -> RibbonItem:
    """A SalesInsight → one RibbonItem, always in `sales`, always
    observer. Pure / duck-typed — reads .id, .severity, .headline,
    .detail. NOTE: the SalesInsight model is 1F (not yet built); the
    router's gather step is import-guarded, so this adapter is only
    actually invoked once 1F lands. Written now so 1C ships the adapter
    logic + un-stubs with a one-line change."""
    severity = getattr(insight, "severity", "info")
    if severity not in _SEVERITY_RANK:
        severity = "info"
    ctx = {
        "text": getattr(insight, "headline", "") or "Sales insight",
        "sub_text": getattr(insight, "detail", None),
    }
    return RibbonItem(
        category="sales", severity=severity, item_type="sales_insight",
        item_id=getattr(insight, "id", 0), deadline_at=None,
        can_dismiss=True, can_check=True, relation="observer",
        source=insight, ctx=ctx,
    )


def _scheduled_event_to_item(event) -> RibbonItem:
    """A ScheduledEvent → one RibbonItem. category is `caterings` or
    `events` per the event's category; always observer; can_check
    False (you don't 'complete' a scheduled event)."""
    category = "caterings" if event.category == "catering" else "events"
    when = _deadline_str(event.scheduled_at)
    ctx = {
        "text": event.title,
        "sub_text": (f"{event.category.title()} — {when}" if when
                     else event.category.title()),
    }
    return RibbonItem(
        category=category, severity="info", item_type="scheduled_event",
        item_id=event.id, deadline_at=event.scheduled_at,
        can_dismiss=True, can_check=False, relation="observer",
        source=event, ctx=ctx,
    )


def _ambient_signal_to_item(signal) -> RibbonItem:
    """An AmbientSignal → one RibbonItem (1J spec §7.1). The six 1J
    /cron/refresh-* crons write AmbientSignal rows; this adapts the
    live ones onto the ribbon's Caterings / Events / Maintenance
    categories — purely additive, alongside the existing adapters.

    Pure — reads .id / .category / .severity / .payload. `category`
    comes straight from AmbientSignal.category, which the model already
    constrains to caterings|events|maintenance (§2/§7) — no _*_TO_RIBBON
    map needed. Always observer (ambient signals have no owner);
    can_check=False — they are observed external state that ages out via
    valid_until_at, not something you "complete" (§7.2, mirrors
    scheduled_event). The payload's headline/detail become the
    render_for text/sub_text."""
    severity = signal.severity if signal.severity in _SEVERITY_RANK else "info"
    payload = signal.payload or {}
    ctx = {
        "text": payload.get("headline") or "Ambient signal",
        "sub_text": payload.get("detail") or None,
    }
    return RibbonItem(
        category=signal.category, severity=severity,
        item_type="ambient_signal", item_id=signal.id, deadline_at=None,
        can_dismiss=True, can_check=False, relation="observer",
        source=signal, ctx=ctx,
    )


# ============================================================
# Pay-masking hook (1H coordination — 1C spec §7.2)
# ============================================================
def _apply_pay_masking(items, user):
    """STUB pending 1H. 1C spec §7.2: any `sales`-category item that
    surfaces labor-cost data must run through 1H's
    render_labor_breakdown(store, user) so manager-tier pay is
    aggregated for non-partner viewers. 1H (render_labor_breakdown) is
    NOT YET BUILT — Sam's locked order puts 1H after 1C. This is a
    pass-through until 1H lands; then wire the helper here.
    TODO(1H): replace with the real render_labor_breakdown call.
    """
    return items


# ============================================================
# ribbon_items_for — the router (1C spec §4)
# ============================================================
def ribbon_items_for(page_slug, user, store_scope, category=None):
    """All ribbon items relevant to (page_slug, user, store_scope),
    ALREADY SORTED (severity DESC, deadline ASC). category=None → all
    seven categories; a slug → just that one.

    Algorithm (1C spec §4):
      1. gather candidate source rows, store-scope-filtered
      2. adapt each into one or two RibbonItems
      3. apply the page-relevance filter
      4. exclude today-dismissed items
      5. filter to `category` if set
      6. sort + return

    Defensive: 1B's ribbon_render_context wraps this in try/except, but
    the router also tolerates user=None (pre-keypad-auth) by returning
    [] — the ribbon never renders for a non-keypad session anyway.
    """
    if user is None or getattr(user, "id", None) is None:
        return []

    role = getattr(user, "permission_level", None)
    user_store = getattr(user, "store_scope", None)
    store_unscoped = role in _STORE_UNSCOPED_ROLES

    def _in_scope(item_store) -> bool:
        """Store-scope filter (1C spec §7.1): store-unscoped roles see
        everything; store-scoped roles see only items whose store
        intersects theirs. A None/blank item store is treated as
        all-stores (visible to everyone) — e.g. a cross-store signal."""
        if store_unscoped:
            return True
        if not item_store:
            return True
        return _store_scopes_intersect(user_store, item_store)

    items: list[RibbonItem] = []
    db = SessionLocal()
    try:
        now = datetime.utcnow()

        # --- 1+2. Tasks → items ---
        # The viewer's own tasks, tasks escalated to them, and (for the
        # observer framing) other in-scope tasks. Open tasks only
        # (completed_at IS NULL). One owner-name lookup batch avoids N+1.
        task_rows = (
            db.query(Task)
            .filter(Task.completed_at.is_(None))
            .all()
        )
        # Pre-resolve owner names for the observer / escalated framing.
        owner_ids = {t.owner_user_id for t in task_rows}
        owner_names = {}
        if owner_ids:
            for u in db.query(User).filter(User.id.in_(owner_ids)).all():
                owner_names[u.id] = u.full_name or f"User {u.id}"
        for t in task_rows:
            is_owner = t.owner_user_id == user.id
            is_escalated_to = t.escalated_to_user_id == user.id
            # store-scope filter: a task's store_scope of "both"/"none"
            # is handled by _in_scope (none → visible). The viewer
            # always sees their own + escalated-to tasks regardless of
            # store (it's theirs); observer tasks must be in scope.
            if not (is_owner or is_escalated_to):
                if not _in_scope(t.store_scope):
                    continue
            items.extend(_task_to_items(
                t, user, None, owner_names.get(t.owner_user_id, "someone")))

        # --- 1+2. Signals → items ---
        # Open (unresolved, unacknowledged) signals in scope.
        signal_rows = (
            db.query(Signal)
            .filter(Signal.resolved_at.is_(None))
            .filter(Signal.acknowledged_at.is_(None))
            .all()
        )
        for s in signal_rows:
            if not _in_scope(s.store_id):
                continue
            items.append(_signal_to_item(s, user))

        # --- 1+2. ScheduledEvents → items ---
        # Upcoming scheduled/confirmed events in scope. Past events +
        # completed/cancelled are skipped (1C concern, not the model's).
        try:
            from app.models import ScheduledEvent
            event_rows = (
                db.query(ScheduledEvent)
                .filter(ScheduledEvent.status.in_(("scheduled", "confirmed")))
                .filter(ScheduledEvent.scheduled_at >= now)
                .all()
            )
            for e in event_rows:
                if not _in_scope(e.store):
                    continue
                items.append(_scheduled_event_to_item(e))
        except ImportError:
            # ScheduledEvent precondition not deployed in this env —
            # skip the source. (Shouldn't happen: 47830e6 shipped it.)
            log.warning("ribbon: ScheduledEvent model unavailable — "
                        "skipping that source")

        # --- 1+2. SalesInsights → items (STUB pending 1F) ---
        # The SalesInsight model is 1F — not yet built. Import-guarded:
        # when 1F lands, this gather starts contributing with no other
        # change. _sales_insight_to_item is already written.
        try:
            from app.models import SalesInsight  # type: ignore
            insight_rows = (
                db.query(SalesInsight)
                .filter(SalesInsight.valid_until_at >= now)
                .all()
            )
            for ins in insight_rows:
                if not _in_scope(getattr(ins, "store_scope", None)):
                    continue
                items.append(_sales_insight_to_item(ins))
        except ImportError:
            pass  # 1F not built yet — sales-insight source is empty.

        # --- 1+2. AmbientSignals → items (1J data plane) ---
        # The gather's fifth source (1J spec §7). The six 1J
        # /cron/refresh-* crons write AmbientSignal rows; this reads the
        # live ones (valid_until_at >= now) and adapts each onto its
        # Caterings / Events / Maintenance category. AmbientSignal is a
        # hard 1J-Day-1 dependency (landed + deploy-live) — unlike the
        # historically import-guarded SalesInsight / ScheduledEvent
        # above, so it imports at module top and needs no guard here.
        ambient_rows = (
            db.query(AmbientSignal)
            .filter(AmbientSignal.valid_until_at >= now)
            .all()
        )
        for sig in ambient_rows:
            if not _in_scope(sig.store_scope):
                continue
            items.append(_ambient_signal_to_item(sig))

        # --- 3. page-relevance filter (1C spec §7.3) ---
        items = _apply_page_relevance(items, page_slug)

        # --- 4. exclude today-dismissed items (1C spec §7.4) ---
        items = _exclude_dismissed(items, user, db)
    finally:
        db.close()

    # --- pay-masking hook (1H coordination — stub) ---
    items = _apply_pay_masking(items, user)

    # --- 5. filter to `category` if set ---
    if category is not None:
        items = [i for i in items if i.category == category]

    # --- 6. sort + return ---
    items.sort(key=_ribbon_sort_key)
    return items


def _apply_page_relevance(items, page_slug):
    """1C spec §7.3 (amendment #1394) — strict page-relevance.
      - "dashboard" slug: all categories, unfiltered — the overview
        intent (the 1B partial hides empty sections per §10).
      - a mapped domain page: that domain's full detail (all
        severities) + the viewer's todo category.
      - an unmapped / unknown slug: empty list — no ribbon. A safe
        blank, NOT a fallback to dashboard behaviour. If a new page
        needs the ribbon, add it to _PAGE_TO_DOMAIN.
    """
    if page_slug not in _PAGE_TO_DOMAIN:
        # unmapped slug → no ribbon (amendment #1394: explicit map
        # only, no implicit fallback).
        return []
    domain = _PAGE_TO_DOMAIN[page_slug]
    if domain is None:
        # "dashboard" sentinel — all categories, unfiltered.
        return items
    # domain page: that domain's full detail + the todo category.
    return [
        i for i in items
        if i.category == domain or i.category == "todo"
    ]


def _exclude_dismissed(items, user, db):
    """1C spec §7.4 — drop any item the viewer dismissed today. One
    up-front query loads today's dismissals into a set, then filter in
    memory (no N+1)."""
    today = date.today().isoformat()
    rows = (
        db.query(RibbonItemDismissal)
        .filter(RibbonItemDismissal.user_id == user.id)
        .filter(RibbonItemDismissal.dismiss_day == today)
        .all()
    )
    dismissed = {(r.item_type, r.item_id) for r in rows}
    if not dismissed:
        return items
    return [
        i for i in items
        if (i.item_type, i.item_id) not in dismissed
    ]
