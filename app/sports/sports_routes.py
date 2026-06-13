"""
sports_routes.py — Flask blueprint for the Sports Tracker.

Mount into the existing dashboard:

    from sports_routes import create_sports_bp
    app.register_blueprint(create_sports_bp(get_conn, default_user="manager"))

`get_conn` is a zero-arg callable returning a DB connection with
row_factory = sqlite3.Row (or an equivalent dict-row factory).

Placeholders use SQLite style (?). The Sports tables are read-mostly cache data
and sit naturally on the SQLite runtime DB; if you mount them on Postgres,
adapt placeholders to %s (or route through your existing DB wrapper).
"""

from flask import Blueprint, jsonify, request

import sports_core as core
from sports_broadcast import resolve_broadcast  # noqa: F401 (used by providers)


def sync_provider(conn, provider, leagues):
    """Fetch each league via a provider and upsert. Returns per-league stats."""
    results = {}
    for lg in leagues:
        try:
            games = provider.fetch(lg)
            results[lg] = core.upsert_games(conn, games, provider=provider.name, league=lg)
        except Exception as exc:  # log and continue; one league must not break others
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """INSERT INTO sports_sync_log
                   (provider, league, run_utc, games_seen, games_inserted,
                    games_updated, games_skipped, status, message)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (provider.name, lg, now, 0, 0, 0, 0, "error", str(exc)[:500]),
            )
            conn.commit()
            results[lg] = {"error": str(exc)}
    return results


def create_sports_bp(get_conn, default_user="manager", url_prefix="/api/sports"):
    bp = Blueprint("sports", __name__, url_prefix=url_prefix)

    TAB_FNS = {
        "today": lambda c, lg, u: core.get_today(c, lg),
        "live": lambda c, lg, u: core.get_live(c, lg),
        "upcoming": lambda c, lg, u: core.get_upcoming(c, league=lg),
        "completed": lambda c, lg, u: core.get_completed(c, league=lg),
        "previous": lambda c, lg, u: core.get_previous(c, league=lg),
        "favorites": lambda c, lg, u: core.get_favorites(c, u, lg),
    }

    @bp.get("/games")
    def games():
        tab = (request.args.get("tab") or "today").lower()
        league = request.args.get("league") or None
        user = request.args.get("user") or default_user
        if tab not in TAB_FNS:
            return jsonify(error=f"unknown tab '{tab}'"), 400
        conn = get_conn()
        data = TAB_FNS[tab](conn, league, user)
        return jsonify(tab=tab, league=league, count=len(data), games=data,
                       generated_ct=core.central_now().strftime("%Y-%m-%d %H:%M:%S %Z"))

    @bp.get("/game/<game_id>")
    def game_detail(game_id):
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM sports_games WHERE provider_game_id = ?", (game_id,)
        ).fetchone()
        if row is None:
            return jsonify(error="not found"), 404
        return jsonify(core.serialize(row))

    @bp.get("/search")
    def search():
        q = (request.args.get("q") or "").strip()
        if len(q) < 2:
            return jsonify(query=q, count=0, games=[])
        conn = get_conn()
        data = core.search_games(conn, q)
        return jsonify(query=q, count=len(data), games=data)

    @bp.get("/counts")
    def counts():
        league = request.args.get("league") or None
        user = request.args.get("user") or default_user
        return jsonify(core.counts(get_conn(), user_id=user, league=league))

    @bp.get("/favorites")
    def favorites():
        user = request.args.get("user") or default_user
        return jsonify(user=user, favorites=core.list_favorites(get_conn(), user))

    @bp.post("/favorite")
    def add_favorite():
        body = request.get_json(force=True, silent=True) or {}
        user = body.get("user") or default_user
        scope, ref = body.get("scope"), body.get("ref")
        if scope not in ("game", "team", "league") or not ref:
            return jsonify(error="scope must be game|team|league and ref is required"), 400
        core.add_favorite(get_conn(), user, scope, ref)
        return jsonify(ok=True, user=user, scope=scope, ref=ref), 201

    @bp.delete("/favorite")
    def remove_favorite():
        body = request.get_json(force=True, silent=True) or {}
        user = body.get("user") or default_user
        scope, ref = body.get("scope"), body.get("ref")
        if not scope or not ref:
            return jsonify(error="scope and ref are required"), 400
        core.remove_favorite(get_conn(), user, scope, ref)
        return jsonify(ok=True)

    @bp.post("/sync")
    def sync():
        """Admin: trigger a fetch. Body: {provider:'espn', leagues:['MLB',...]}."""
        body = request.get_json(force=True, silent=True) or {}
        leagues = body.get("leagues") or []
        provider_name = (body.get("provider") or "espn").lower()
        if provider_name == "espn":
            from sports_provider import EspnProvider
            provider = EspnProvider()
        else:
            return jsonify(error=f"unsupported provider '{provider_name}'"), 400
        results = sync_provider(get_conn(), provider, leagues)
        return jsonify(provider=provider_name, results=results)

    @bp.get("/health")
    def health():
        conn = get_conn()
        rows = conn.execute(
            "SELECT provider, league, run_utc, games_seen, games_inserted, "
            "games_updated, games_skipped, status, message "
            "FROM sports_sync_log ORDER BY run_utc DESC LIMIT 20"
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM sports_games").fetchone()["n"]
        return jsonify(total_games=total, recent_syncs=[dict(r) for r in rows],
                       server_ct=core.central_now().strftime("%Y-%m-%d %H:%M:%S %Z"))

    return bp
