"""Ribbon routes — Phase 2 / Block 1 / sub-block 1B (ck, 2026-05-14).

Ships:
  - POST /partner/ribbon/collapse/<category> — the collapse-toggle
    endpoint for the per-user per-category collapse preference.
  - ribbon_render_context() — the DEFENSIVE presentation-layer
    wrapper the _ribbon.html partial calls (see the note below).
  - install() — registers the blueprint + two Jinja globals.

Why ribbon_render_context() exists (deviation-with-rationale from
1B spec §4, flagged for samai review):
  The 1B spec §4 says the partial "Calls the router once:
  {{ ribbon_items_for(...) }}" AND that "the entire partial body is
  wrapped so that any failure ... renders an empty ribbon and logs at
  WARN ... it must never 500 a page." Those two can't both be done in
  pure Jinja — Jinja has no try/except, so a router that raises (or an
  item missing an attribute) inside a {% for %} WOULD 500 the page.
  So the defensive wrapper has to be Python. ribbon_render_context()
  is that wrapper: it calls 1C's ribbon_items_for(), groups by
  category, pre-renders every item's render_for() payload, folds in
  the per-user collapse prefs, and try/excepts the whole thing —
  returning a fully-safe, fully-pre-computed structure so the partial
  is pure dumb iteration with zero failure points. ribbon_items_for
  is still registered as the stub Jinja global per spec §11 Q1 and
  stays 1C's contract surface; the partial just doesn't call it raw.

1B scope boundary:
  - HERE: collapse endpoint + ribbon_render_context + Jinja wiring.
  - 1C (aick): the real ribbon_items_for content-router body in
    app/services/ribbon.py. ribbon_render_context does a fresh module
    attribute lookup at call time, so when 1C replaces the stub the
    wrapper picks it up with no change here.
  - 1D (ck, next): the X/Check dismiss/check endpoints + ribbon.js
    that make the inert markup live.
"""
from __future__ import annotations

import logging

from flask import Blueprint, g, jsonify, redirect, request, url_for

from app.db import SessionLocal
from app.models import RibbonCategoryPreference
from app.services.ribbon import (
    RIBBON_CATEGORIES,
    RIBBON_CATEGORY_SLUGS,
    ribbon_items_for,
)

log = logging.getLogger(__name__)

ribbon_bp = Blueprint("ribbon", __name__)


# ============================================================
# Collapse-toggle endpoint
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
# Defensive presentation-layer wrapper (see module docstring)
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

      - ribbon_items_for : the stub content router from
        app.services.ribbon (spec §11 Q1). Returns [] during the
        1B/1C parallel window; 1C replaces the body. Registered so
        templates / tooling can call the router directly if needed.
      - ribbon_render_context : 1B's defensive wrapper — what
        _ribbon.html actually calls. See the module docstring.
    """
    app.register_blueprint(ribbon_bp)
    app.jinja_env.globals["ribbon_items_for"] = ribbon_items_for
    app.jinja_env.globals["ribbon_render_context"] = ribbon_render_context
