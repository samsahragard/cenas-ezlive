"""Notifications page — Sam-approved replacement for the universal top-of-page
ribbon (dck mockup 5dd0088 + 3f6745e gate-fold; cena #2569 + #2628 + #2641
two-commit + behavior-parity-gate discipline).

COMMIT 1 (this module): /partner/notifications route + ribbon_render_context()
data reuse + dck's template. Ribbon code is NOT touched here — the existing
{% include 'partials/_ribbon.html' %} in base_dashboard.html stays live as the
parity-test baseline. After Sam validates a real operational beat on live (alert
lands → visible in a notifications tab → dismiss via X → unread badge
decrements + ribbon reflects same state), commit 2 retires the ribbon code in
a separate PR. Do NOT bundle ribbon removal into this commit.

Dismiss is reused from ribbon_routes.py:241 — POST
/partner/ribbon/dismiss/<item_type>/<item_id>. The notif-card X button fires
the same endpoint with the same (item_type, item_id) tuple that
ribbon_render_context() emits per entry. Same shape, same backing store
(ribbon_item_dismissals table per plan §2.3).
"""
from __future__ import annotations

import logging

from flask import Blueprint, g, redirect, render_template, url_for

from app.services.ribbon import RIBBON_CATEGORIES
from app.web.ribbon_routes import ribbon_render_context

log = logging.getLogger(__name__)

notifications_bp = Blueprint("notifications", __name__)


@notifications_bp.route("/partner/notifications", methods=["GET"])
def notifications_page():
    """Notifications dashboard. All 7 ribbon categories rendered as tabs;
    cards inside each tab come from ribbon_render_context().

    Auth: relies on the existing partner_auth gate via the partner-session
    cookie. The base layout already enforces sign-in; if we land here without
    g.current_user the ribbon context degrades to the all-empty fallback.
    """
    from flask import session
    if not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login"))

    user = getattr(g, "current_user", None)
    # store_scope=None for the partner-tier rollup (v1 per dck mockup notes);
    # store-scoped version (`/<store>/notifications`) is a v2 follow-up.
    categories = ribbon_render_context(user, "notifications", None)

    total_count = sum((cat.get("count") or 0) for cat in categories)
    return render_template(
        "notifications.html",
        active="notifications",
        categories=categories,
        category_meta=RIBBON_CATEGORIES,
        total_count=total_count,
    )


def install(app):
    """Register the notifications blueprint."""
    app.register_blueprint(notifications_bp)
