"""Employee Sports tab -- the "What's On" board for staff (Sam 2026-06-13).

A 6th employee bottom-nav tab (right of "You") that reuses the manager Sports
Board template (sports_dashboard.html) + the same ESPN live feed
(app.sports.live_feed.get_live_games). The board content is store-agnostic
(both stores are Houston, same channels), so no store scoping is needed. The
page renders the board WITH the employee bottom nav (employee_nav=True) and
points the board's feed at the employee data endpoint, so the manager-gated
/<store>/sports/data.json stays untouched.

Attaches to the employee_auth blueprint (decorator side effect; imported in
app/__init__.py before ezempauth.install). /employee/sports is added to
auth.py EXEMPT_PREFIXES so the page self-guards session['employee_id'] (302 to
/employee/login) and the data endpoint returns its own 401 JSON.
"""
from __future__ import annotations

from flask import jsonify, redirect, render_template, session

from app.web.employee_auth import employee_auth


@employee_auth.route("/employee/sports", methods=["GET"])
def employee_sports_page():
    """The Sports Board on the employee console (bottom-nav tab)."""
    if not session.get("employee_id"):
        return redirect("/employee/login")
    return render_template(
        "sports_dashboard.html",
        employee_nav=True,
        active_nav="sports",
        feed_url="/employee/sports/data.json",
    )


@employee_auth.route("/employee/sports/data.json", methods=["GET"])
def employee_sports_data():
    """Live 'what's on' feed for the employee Sports Board -- same ESPN games +
    Houston broadcast mapping the manager board uses; on any failure returns
    ok:false so the board keeps its built-in sample slate."""
    if not session.get("employee_id"):
        return jsonify({"ok": False, "error": "login required"}), 401
    try:
        from app.sports.live_feed import get_live_games
        games, meta = get_live_games()
        return jsonify({"ok": True, "sample": False, "games": games, "meta": meta})
    except Exception:
        return jsonify({"ok": False, "sample": True, "games": []})
