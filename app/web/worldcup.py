"""
worldcup.py — PUBLIC, no-login World Cup page.

A standalone public view at /worldcup (no store scope, no auth — exempted in
auth.py EXEMPT_PREFIXES) that reuses the Sports Board UI but shows ONLY FIFA
World Cup games. Anyone with the link can open it. It serves only public sports
data (schedule, scores, where-to-watch) — no store-internal or personal data.

Reuses the same live feed + verification as the manager Sports tab; just filters
to the World Cup and serves it unauthenticated.
"""

import logging

from flask import Blueprint, render_template, jsonify

worldcup_bp = Blueprint("worldcup", __name__)

_WORLD_CUP_LEAGUE = "FIFA World Cup"


@worldcup_bp.route("/worldcup", strict_slashes=False)
def worldcup_page():
    """Public World Cup board. The shared template renders in 'worldcup' mode
    (World Cup branding, single-league, sport filters hidden) and fetches the
    public /worldcup/data.json feed below."""
    return render_template("sports_dashboard.html", worldcup=True)


@worldcup_bp.route("/worldcup/data.json")
def worldcup_data():
    """Public feed: the live games filtered to the FIFA World Cup. Same cached
    ESPN sweep + official-source verification as the manager board; on any
    failure returns ok:false so the page keeps its built-in sample slate."""
    try:
        from app.sports.live_feed import get_live_games
        games, meta = get_live_games()
        wc = [g for g in games if g.get("league") == _WORLD_CUP_LEAGUE]
        return jsonify({
            "ok": True, "sample": False, "games": wc,
            "meta": {**meta, "count": len(wc),
                     "verified": sum(1 for g in wc if g.get("verified"))},
        })
    except Exception:
        logging.getLogger(__name__).exception("worldcup feed failed")
        return jsonify({"ok": False, "sample": True, "games": [], "meta": {}})
