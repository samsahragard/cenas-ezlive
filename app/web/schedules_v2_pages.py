"""Schedules V2 - Block 4: the manager week-view PAGE (frontend shell, ck).

This is the HTML page for the draft week-builder. It rides store_bp (the
/<store_slug>/ prefix) so it inherits the same gates as aick's data
endpoints in schedules_v2.py:
  - _pull_store sets g.current_store / g.current_location / g.store_label
    (404 on an unknown slug),
  - _per_store_gate blocks a single-store manager from the OTHER store's
    URL before the view runs (cross-store gm -> 302 redirect to their own
    store, never a 200 with foreign data),
  - the partner second-factor gate covers /partner/schedules-v2/.
On top of that @require_level("foh_manager") matches the read/write
endpoints exactly, so the same manager audience that can read the board +
create drafts can open this page; expo/driver -> 403 and employees (SMS
session, no keypad user) -> redirected to keypad-login (drafts stay
invisible to employees).

The page touches NO models - aick owns that lane. It hands the template a
small JSON config (store label + the four endpoint PATHS) and the client JS
fetches everything from the board endpoint. The endpoint URLs are built as
/<slug>/schedules-v2/... path strings (the same house pattern store_routes
uses for its sub-nav hrefs) so this page does not depend on aick's exact
view-function names and renders even before the board endpoint is present
in a given tree.
"""
from __future__ import annotations

import json

from flask import g, render_template

from app.web.permissions import require_level
from app.web.store_routes import store_bp

_MGR = "foh_manager"  # mirror schedules_v2.py's manager gate


@store_bp.route("/schedules-v2/", methods=["GET"])
@require_level(_MGR)
def sv2_week_page():
    """Render the client-side week-builder. All data comes from the board
    endpoint via fetch(); this view only assembles the config blob."""
    slug = g.current_store
    base = f"/{slug}/schedules-v2"
    config = {
        "storeLabel": g.store_label,
        "boardUrl": f"{base}/board",
        "scheduleNewUrl": f"{base}/schedule/new",
        "shiftNewUrl": f"{base}/shifts/new",
        "shiftUrlBase": f"{base}/shifts",
    }
    return render_template(
        "schedules_v2_week.html",
        store_label=g.store_label,
        config_json=json.dumps(config),
    )
