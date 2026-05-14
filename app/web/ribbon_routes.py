"""Ribbon routes — Phase 2 / Block 1, sub-blocks 1B + 1D (ck).

1B (2026-05-14) shipped:
  - POST /partner/ribbon/collapse/<category> — collapse-toggle endpoint
  - ribbon_render_context() — the defensive presentation-layer wrapper
    the _ribbon.html partial calls (see the note below)
  - install() — registers the blueprint + Jinja globals

1D (2026-05-14) adds:
  - POST /partner/ribbon/dismiss/<item_type>/<item_id> — the X handler
  - POST /partner/ribbon/check/<item_type>/<item_id>   — the Check handler
  These make the inert X/Check markup 1B rendered actually live; the
  client side is app/static/js/ribbon.js.

Why ribbon_render_context() exists (deviation-with-rationale from
1B spec §4, samai-reviewed + spec-amended):
  The 1B spec §4 wanted the partial to both "call the router once" AND
  be "wrapped so any failure renders empty + never 500s." Impossible in
  pure Jinja (no try/except). So the defensive wrapper is Python:
  ribbon_render_context() calls 1C's ribbon_items_for(), groups by
  category, pre-renders every render_for() payload, folds in collapse
  prefs, and try/excepts the whole path — the partial is pure dumb
  iteration. samai reviewed this as "strictly better" and amended
  spec §4 (31a297b).

1D audience-eligibility design (samai rule 1 — audience-eligibility-
before-mutation — applied proportional to blast radius):
  - DISMISS is self-scoped: it writes RibbonItemDismissal(user_id=ME),
    which only hides the item from the dismisser's OWN ribbon for the
    rest of today. A "wrong" dismiss has zero blast radius beyond the
    dismisser's own day-view. So dismiss eligibility is a light check:
    valid item_type + the referenced row actually exists (don't
    accrue dismissal rows for garbage ids).
  - CHECK is the real mutation surface: a task check stamps
    completed_at globally (feeds the escalation cron + the team-tab
    miss-rate reports); a signal check acks it for everyone. So check
    gets the full per-item-type audience check — task: owner /
    escalated-to / partner only; signal: the anomaly-audience check
    (role ∈ audience_roles + store scope); scheduled_event: rejected
    (you don't "complete" a scheduled event — can_check is always
    False for it); sales_insight: import-guarded, 1F not built yet.

1D scope boundary — NOT here: the escalation cron (1E), sales-insight
production (1F). The sales_insight branches in both handlers are
import-guarded so they un-stub automatically when 1F lands.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from flask import Blueprint, g, jsonify, redirect, request, url_for

from app.db import SessionLocal
from app.models import (
    RibbonCategoryPreference,
    RibbonItemDismissal,
    Task,
    TaskAuditLog,
    Signal,
    SignalAck,
    ScheduledEvent,
)
from app.services.ribbon import (
    RIBBON_CATEGORIES,
    RIBBON_CATEGORY_SLUGS,
    ribbon_items_for,
)

log = logging.getLogger(__name__)

ribbon_bp = Blueprint("ribbon", __name__)

# The four item_types the ribbon renders (1C contract — task / signal /
# sales_insight were the original three; scheduled_event was added by
# 1C's §13 contract change). The dismiss/check <item_type> URL param is
# validated against this set.
_VALID_RIBBON_ITEM_TYPES = frozenset(
    {"task", "signal", "sales_insight", "scheduled_event"}
)


# ============================================================
# Collapse-toggle endpoint (1B)
# ============================================================
@ribbon_bp.route("/partner/ribbon/collapse/<category>", methods=["POST"])
def ribbon_collapse(category: str):
    """Toggle the current user's collapse preference for one ribbon
    category. Upsert: no row → INSERT is_collapsed=True; existing row →
    flip is_collapsed. Returns JSON {ok, category, is_collapsed} so the
    chevron updates without a page reload.

    Authenticated-session guard only — no permission tag. Per 1B spec
    §8: the ribbon is role-aware but not role-restricted, and collapse
    state is pure per-user UI preference. Any keypad-authed user may
    collapse their own ribbon.
    """
    user = getattr(g, "current_user", None)
    if user is None:
        # Not keypad-authed — bounce to login, mirroring the
        # require_level / requires_permission redirect shape.
        return redirect(url_for("keypad_auth.login", next=request.path))
    if category not in RIBBON_CATEGORY_SLUGS:
        return jsonify({"ok": False, "error": "unknown ribbon category"}), 400
    db = SessionLocal()
    try:
        pref = (
            db.query(RibbonCategoryPreference)
            .filter(
                RibbonCategoryPreference.user_id == user.id,
                RibbonCategoryPreference.category == category,
            )
            .first()
        )
        if pref is None:
            # First toggle for this (user, category): default state is
            # expanded (no row), so the first POST collapses it.
            pref = RibbonCategoryPreference(
                user_id=user.id, category=category, is_collapsed=True,
            )
            db.add(pref)
        else:
            pref.is_collapsed = not pref.is_collapsed
        db.commit()
        return jsonify({
            "ok": True,
            "category": category,
            "is_collapsed": pref.is_collapsed,
        })
    finally:
        db.close()


# ============================================================
# X / Check handlers (1D)
# ============================================================
def _load_item_row(db, item_type: str, item_id: int):
    """Load the underlying row for a ribbon item. Returns the row or
    None if it doesn't exist. sales_insight is import-guarded — its
    model (SalesInsight) is 1F, not built yet; until then no
    sales_insight item can be on any ribbon, so a sales_insight id is
    treated as a non-existent row."""
    if item_type == "task":
        return db.get(Task, item_id)
    if item_type == "signal":
        return db.get(Signal, item_id)
    if item_type == "scheduled_event":
        return db.get(ScheduledEvent, item_id)
    if item_type == "sales_insight":
        try:
            from app.models import SalesInsight  # 1F — may not exist yet
        except ImportError:
            return None
        return db.get(SalesInsight, item_id)
    return None


def _can_check_item(db, user, item_type: str, item_id: int, row) -> tuple[bool, str]:
    """Audience-eligibility for the CHECK action (the high-blast-radius
    mutation). Returns (eligible, reason). `row` is the already-loaded
    underlying row (guaranteed non-None by the caller).

    Per item type:
      - task: only the owner, the escalated-to manager, or a partner
        may mark a task complete. A manager who merely *sees* a
        subordinate's task as observer-framing cannot complete it —
        that matches 1C's RibbonItem.can_check, which is True only for
        the owner_todo / escalated_to relations.
      - signal: the anomaly-audience check — the user's role must be in
        the signal's audience_roles (empty audience = visible to all),
        and the signal's store must match the user's scope (NULL
        store_id = global). Mirrors (does not import — the helpers are
        anomaly_routes-private) the /partner/anomalies/<id>/ack check;
        flagged for samai as a shared-helper-extraction follow-up
        candidate.
      - scheduled_event: never checkable — you don't "complete" a
        scheduled event (1C sets can_check=False for it).
      - sales_insight: 1F not built; caller import-guards before here.
    """
    role = (getattr(user, "permission_level", "") or "").strip()

    if item_type == "task":
        if role == "partner":
            return True, "partner"
        if user.id == row.owner_user_id:
            return True, "owner"
        if row.escalated_to_user_id is not None and user.id == row.escalated_to_user_id:
            return True, "escalated_to"
        return False, "not the task owner / escalation target"

    if item_type == "signal":
        aud = row.audience_roles or []
        if aud and role not in aud and role != "partner":
            return False, "not in this signal's audience"
        # Store scope: NULL store_id = global signal. Otherwise the
        # signal's store must be covered by the user's store_scope.
        # partner / store-unscoped roles have cross-store visibility.
        if row.store_id:
            user_store = (getattr(user, "store_scope", "") or "")
            if role != "partner" and row.store_id not in user_store:
                return False, "signal is scoped to a different store"
        return True, "in audience"

    if item_type == "scheduled_event":
        return False, "scheduled events cannot be marked complete"

    if item_type == "sales_insight":
        # Reached only if SalesInsight exists (caller import-guards the
        # row load). Any keypad user who can see the sales category may
        # dismiss-permanently an insight — it's per-store info, not
        # role-restricted. 1F finalizes this when it lands.
        return True, "sales insight"

    return False, "unknown item type"


@ribbon_bp.route(
    "/partner/ribbon/dismiss/<item_type>/<int:item_id>", methods=["POST"])
def ribbon_dismiss(item_type: str, item_id: int):
    """X handler — dismiss a ribbon item for the rest of today.

    Writes a RibbonItemDismissal(user_id, item_type, item_id,
    dismiss_day=date.today().isoformat()). 1C's _exclude_dismissed
    reads against exactly that (item_type, item_id, today) tuple, so
    the item drops from the user's ribbon on the next render and comes
    back tomorrow for re-triage.

    Idempotent: the uq(user_id, item_type, item_id, dismiss_day)
    constraint means a double-dismiss in one day is a harmless no-op —
    we check-then-insert and return 200 either way.

    Eligibility (proportional — see module docstring): dismiss is
    self-scoped, so the check is light — valid item_type + the
    referenced row exists. A dismiss can only ever affect the
    dismisser's own day-view.

    One-source-two-items note (aick's 1C contract): a task can render
    as two RibbonItems (owner_todo in `todo` + observer in its domain
    category). Both carry the same (item_type, item_id), so one dismiss
    POST + 1C's (item_type, item_id)-keyed exclusion drops both copies.
    """
    user = getattr(g, "current_user", None)
    if user is None:
        return jsonify({"ok": False, "error": "not signed in"}), 401
    if item_type not in _VALID_RIBBON_ITEM_TYPES:
        return jsonify({"ok": False, "error": "unknown item type"}), 400

    db = SessionLocal()
    try:
        row = _load_item_row(db, item_type, item_id)
        if row is None:
            # No such item — don't accrue dismissal rows for garbage ids.
            return jsonify({"ok": False, "error": "item not found"}), 404

        today = date.today().isoformat()
        existing = (
            db.query(RibbonItemDismissal)
            .filter(
                RibbonItemDismissal.user_id == user.id,
                RibbonItemDismissal.item_type == item_type,
                RibbonItemDismissal.item_id == item_id,
                RibbonItemDismissal.dismiss_day == today,
            )
            .first()
        )
        if existing is not None:
            # Already dismissed today — idempotent no-op.
            return jsonify({
                "ok": True, "item_type": item_type, "item_id": item_id,
                "dismissed": True, "already": True,
            })

        db.add(RibbonItemDismissal(
            user_id=user.id,
            item_type=item_type,
            item_id=item_id,
            dismiss_day=today,
            dismissed_at=datetime.utcnow(),
        ))
        db.commit()
        return jsonify({
            "ok": True, "item_type": item_type, "item_id": item_id,
            "dismissed": True, "already": False,
        })
    finally:
        db.close()


@ribbon_bp.route(
    "/partner/ribbon/check/<item_type>/<int:item_id>", methods=["POST"])
def ribbon_check(item_type: str, item_id: int):
    """Check handler — mark a ribbon item complete / acknowledged.

      - task          → set completed_at + completed_by_user_id, write
                        a TaskAuditLog(action="completed") row.
      - signal        → stamp Signal.acknowledged_by/at (first ack
                        only) + write a SignalAck row. Mirrors
                        /partner/anomalies/<id>/ack.
      - sales_insight → "dismisses permanently" per the directive —
                        append the user to SalesInsight.dismissed_by.
                        Import-guarded (1F not built); 503 until then.
      - scheduled_event → rejected; you don't complete a scheduled
                        event (can_check is always False for it).

    Eligibility: the full per-item-type audience check (see
    _can_check_item) — check is the high-blast-radius mutation, so
    unlike dismiss it gets the real gate.
    """
    user = getattr(g, "current_user", None)
    if user is None:
        return jsonify({"ok": False, "error": "not signed in"}), 401
    if item_type not in _VALID_RIBBON_ITEM_TYPES:
        return jsonify({"ok": False, "error": "unknown item type"}), 400

    db = SessionLocal()
    try:
        # sales_insight: import-guard before anything else — its model
        # is 1F, not built. No sales_insight item can be on a ribbon
        # yet, so a check POST for one is "not available" until 1F.
        if item_type == "sales_insight":
            try:
                from app.models import SalesInsight  # noqa: F401  (1F)
            except ImportError:
                return jsonify({
                    "ok": False,
                    "error": "sales insights not available yet (Block 1F)",
                }), 503

        row = _load_item_row(db, item_type, item_id)
        if row is None:
            return jsonify({"ok": False, "error": "item not found"}), 404

        eligible, reason = _can_check_item(db, user, item_type, item_id, row)
        if not eligible:
            return jsonify({"ok": False, "error": reason}), 403

        now = datetime.utcnow()

        if item_type == "task":
            if row.completed_at is not None:
                # Already complete — idempotent no-op, don't double-audit.
                return jsonify({
                    "ok": True, "item_type": item_type, "item_id": item_id,
                    "checked": True, "already": True,
                })
            row.completed_at = now
            row.completed_by_user_id = user.id
            # TaskAuditLog "completed" — 1A reserved this enum value for
            # the sub-block that owns the complete path (1D). details
            # shape per 1A spec §7.
            db.add(TaskAuditLog(
                task_id=row.id,
                actor_user_id=user.id,
                action="completed",
                details={"completed_by_user_id": user.id},
            ))
            db.commit()
            return jsonify({
                "ok": True, "item_type": item_type, "item_id": item_id,
                "checked": True, "already": False,
            })

        if item_type == "signal":
            # Stamp the first ack only — later checks still log a
            # SignalAck row (captures the second interaction) but the
            # signal's acknowledged_at sticks at the first. Mirrors the
            # /partner/anomalies/<id>/ack endpoint.
            already = row.acknowledged_at is not None
            if not already:
                row.acknowledged_by = user.id
                row.acknowledged_at = now
            db.add(SignalAck(
                signal_id=row.id,
                user_id=user.id,
                acked_at=now,
                note="ribbon check",
            ))
            db.commit()
            return jsonify({
                "ok": True, "item_type": item_type, "item_id": item_id,
                "checked": True, "already": already,
            })

        if item_type == "sales_insight":
            # 1F-guarded above; once SalesInsight lands this appends
            # user.id to its dismissed_by JSON list (permanent dismiss
            # per the directive's 1D Check semantics for insights).
            dismissed_by = list(getattr(row, "dismissed_by", None) or [])
            if user.id not in dismissed_by:
                dismissed_by.append(user.id)
                row.dismissed_by = dismissed_by
            db.commit()
            return jsonify({
                "ok": True, "item_type": item_type, "item_id": item_id,
                "checked": True, "already": False,
            })

        # scheduled_event reaches here only if _can_check_item somehow
        # passed it — it doesn't, but belt-and-suspenders.
        return jsonify({"ok": False, "error": "item type not checkable"}), 400
    finally:
        db.close()


# ============================================================
# Defensive presentation-layer wrapper (1B — see module docstring)
# ============================================================
def _empty_categories():
    """The all-empty, all-expanded fallback structure — what the
    partial renders when anything in the render path fails. Seven
    categories, fixed order, zero items, nothing collapsed.

    Note the key is "entries", not "items": Jinja resolves `cat.items`
    to the dict.items() builtin method, not a dict key — so the
    per-category item list MUST be under a non-colliding key. The
    partial iterates `cat.entries`."""
    return [
        {
            "slug": slug,
            "label": label,
            "is_collapsed": False,
            "count": 0,
            "entries": [],
        }
        for slug, label in RIBBON_CATEGORIES
    ]


def ribbon_render_context(user, page_slug, store_scope):
    """Build the fully-safe, fully-pre-computed structure the
    _ribbon.html partial iterates. NEVER raises — any failure logs at
    WARN and returns the all-empty fallback so the ribbon degrades to
    an empty shell rather than 500-ing the page it's embedded in.

    Returns a list of seven category dicts in RIBBON_CATEGORIES order:
        {
          "slug":         str,   # ribbon category slug
          "label":        str,   # display label
          "is_collapsed": bool,  # this user's collapse pref (default False)
          "count":        int,   # number of entries in this category
          "entries": [           # pre-rendered, safe to dumb-iterate
            {
              "item_type":  str,
              "item_id":    int,
              "can_dismiss": bool,
              "can_check":   bool,
              "text":        str,
              "sub_text":    str | None,
              "styling_class": str,
            }, ...
          ],
        }

    The per-category list is keyed "entries" not "items" — see
    _empty_categories() for why (Jinja dict.items collision).
    """
    try:
        # Fresh module attribute lookup so we pick up 1C's real router
        # once it replaces the stub in app/services/ribbon.py.
        from app.services import ribbon as _ribbon_mod
        raw_items = _ribbon_mod.ribbon_items_for(page_slug, user, store_scope)

        # Per-user collapse prefs → {category_slug: is_collapsed}.
        collapsed = {}
        if user is not None and getattr(user, "id", None) is not None:
            db = SessionLocal()
            try:
                rows = (
                    db.query(RibbonCategoryPreference)
                    .filter(RibbonCategoryPreference.user_id == user.id)
                    .all()
                )
                collapsed = {r.category: r.is_collapsed for r in rows}
            finally:
                db.close()

        # Group items into the seven fixed buckets. Items whose
        # category isn't a known slug are dropped (defensive — a
        # mis-tagged item can't break the layout).
        buckets: dict[str, list] = {slug: [] for slug, _ in RIBBON_CATEGORIES}
        for item in (raw_items or []):
            cat = getattr(item, "category", None)
            if cat not in buckets:
                log.warning(
                    "ribbon: dropping item with unknown category %r "
                    "(item_type=%r item_id=%r)",
                    cat,
                    getattr(item, "item_type", None),
                    getattr(item, "item_id", None),
                )
                continue
            # Pre-render the item's payload here, inside the try, so a
            # render_for() that raises degrades to a dropped item +
            # WARN rather than a 500 inside the Jinja loop.
            try:
                payload = item.render_for(user)
                buckets[cat].append({
                    "item_type": item.item_type,
                    "item_id": item.item_id,
                    "can_dismiss": bool(item.can_dismiss),
                    "can_check": bool(item.can_check),
                    "text": payload.get("text", ""),
                    "sub_text": payload.get("sub_text"),
                    "styling_class": payload.get("styling_class", ""),
                })
            except Exception:
                log.warning(
                    "ribbon: render_for() failed for item_type=%r "
                    "item_id=%r — dropping item",
                    getattr(item, "item_type", None),
                    getattr(item, "item_id", None),
                    exc_info=True,
                )

        return [
            {
                "slug": slug,
                "label": label,
                "is_collapsed": bool(collapsed.get(slug, False)),
                "count": len(buckets[slug]),
                "entries": buckets[slug],
            }
            for slug, label in RIBBON_CATEGORIES
        ]
    except Exception:
        # The whole point of 1B spec §9's most-important test: a
        # ribbon bug cannot take down every authenticated page.
        log.warning(
            "ribbon: render context build failed — degrading to empty "
            "ribbon (page_slug=%r)", page_slug, exc_info=True,
        )
        return _empty_categories()


# ============================================================
# install()
# ============================================================
def install(app):
    """Register the ribbon blueprint + the two Jinja globals. Called
    from app.create_app() after the blueprint imports settle (mirrors
    ezanomaly.install / ezperms.install).

      - ribbon_items_for : the content router from app.services.ribbon
        (1C's real implementation; was a stub during the 1B/1C parallel
        window). Registered so templates / tooling can call it directly.
      - ribbon_render_context : 1B's defensive wrapper — what
        _ribbon.html actually calls. See the module docstring.

    The dismiss/check endpoints (1D) ride the same ribbon_bp blueprint,
    so registering it here wires them too.
    """
    app.register_blueprint(ribbon_bp)
    app.jinja_env.globals["ribbon_items_for"] = ribbon_items_for
    app.jinja_env.globals["ribbon_render_context"] = ribbon_render_context
