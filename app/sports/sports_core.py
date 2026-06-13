"""
sports_core.py — the Sports Tracker engine (pure logic, stdlib only).

Responsibilities:
  * Timezone: store UTC, display Houston/Central (America/Chicago, DST-aware).
  * Dedup: upsert games against UNIQUE(provider, provider_game_id); a game that
    was already shown is updated in place, never re-inserted. content_hash makes
    unchanged games a no-op so live tiles only re-render when something moves.
  * Categories: Today / Live / Upcoming / Completed / Previous / Favorites,
    with "Today" computed against the current Houston date so it resets itself
    at Central midnight with no cron job.

No third-party imports — this module is unit-testable offline.
"""

import sqlite3
import hashlib
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")  # Houston — handles CST/CDT automatically

# Normalized status vocabulary (providers map their states onto these)
STATUS_SCHEDULED = "scheduled"
STATUS_PRE = "pre"
STATUS_LIVE = "in_progress"
STATUS_HALFTIME = "halftime"
STATUS_DELAYED = "delayed"
STATUS_POSTPONED = "postponed"
STATUS_FINAL = "final"
STATUS_CANCELED = "canceled"

LIVE_STATES = (STATUS_LIVE, STATUS_HALFTIME, STATUS_DELAYED)
UPCOMING_STATES = (STATUS_SCHEDULED, STATUS_PRE)

# Fields that can change after a game first appears. The hash of these decides
# whether an upsert is a real update or a skip.
_MUTABLE_FIELDS = (
    "status", "status_detail", "start_utc",
    "home_score", "away_score", "tv_national", "broadcast_json",
)


# --------------------------------------------------------------------------- #
# Time helpers (UTC in, Central out)
# --------------------------------------------------------------------------- #
def _parse_utc(iso):
    """Parse an ISO-8601 string (with Z or offset) into an aware UTC datetime."""
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_central(iso):
    """UTC ISO string -> aware datetime in Houston/Central time."""
    return _parse_utc(iso).astimezone(CENTRAL)


def central_now():
    return datetime.now(timezone.utc).astimezone(CENTRAL)


def central_today():
    """Current calendar date in Houston. The anchor for the Today tab."""
    return central_now().date()


def format_central(iso):
    """Human label, e.g. 'Sat, Jun 13 · 2:08 PM CDT'."""
    dt = to_central(iso)
    return dt.strftime("%a, %b %-d · %-I:%M %p %Z")


def central_fields(iso):
    """Display components used by the UI."""
    dt = to_central(iso)
    return {
        "ct_label": format_central(iso),
        "ct_time": dt.strftime("%-I:%M %p"),
        "ct_day": dt.strftime("%a"),
        "ct_date": dt.strftime("%b %-d"),
        "ct_iso_date": dt.strftime("%Y-%m-%d"),
        "ct_tz": dt.strftime("%Z"),
    }


# --------------------------------------------------------------------------- #
# Hashing / dedup
# --------------------------------------------------------------------------- #
def content_hash(g):
    payload = "|".join(str(g.get(f, "")) for f in _MUTABLE_FIELDS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def connect(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn, schema_path="sports_schema.sql"):
    with open(schema_path, "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    conn.commit()


def upsert_games(conn, games, provider="fixture", league=None):
    """
    Insert new games and update changed ones. Returns a stats dict and writes a
    row to sports_sync_log. This IS the duplicate-checking layer.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = updated = skipped = 0

    for g in games:
        g = dict(g)
        if isinstance(g.get("broadcast_json"), (dict, list)):
            g["broadcast_json"] = json.dumps(g["broadcast_json"], sort_keys=True)
        h = content_hash(g)

        row = conn.execute(
            "SELECT id, content_hash FROM sports_games WHERE provider=? AND provider_game_id=?",
            (provider, g["provider_game_id"]),
        ).fetchone()

        if row is None:
            conn.execute(
                """INSERT INTO sports_games
                   (provider, provider_game_id, sport, league, season, status,
                    status_detail, start_utc, home_team, home_abbr, home_score,
                    home_color, away_team, away_abbr, away_score, away_color,
                    venue, venue_city, tv_national, broadcast_json,
                    content_hash, first_seen_utc, last_updated_utc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (provider, g["provider_game_id"], g.get("sport"), g.get("league"),
                 g.get("season"), g.get("status"), g.get("status_detail"),
                 g["start_utc"], g.get("home_team"), g.get("home_abbr"),
                 g.get("home_score"), g.get("home_color"), g.get("away_team"),
                 g.get("away_abbr"), g.get("away_score"), g.get("away_color"),
                 g.get("venue"), g.get("venue_city"), g.get("tv_national"),
                 g.get("broadcast_json"), h, now, now),
            )
            inserted += 1
        elif row["content_hash"] != h:
            conn.execute(
                """UPDATE sports_games SET
                     sport=?, league=?, season=?, status=?, status_detail=?,
                     start_utc=?, home_team=?, home_abbr=?, home_score=?,
                     home_color=?, away_team=?, away_abbr=?, away_score=?,
                     away_color=?, venue=?, venue_city=?, tv_national=?,
                     broadcast_json=?, content_hash=?, last_updated_utc=?
                   WHERE id=?""",
                (g.get("sport"), g.get("league"), g.get("season"), g.get("status"),
                 g.get("status_detail"), g["start_utc"], g.get("home_team"),
                 g.get("home_abbr"), g.get("home_score"), g.get("home_color"),
                 g.get("away_team"), g.get("away_abbr"), g.get("away_score"),
                 g.get("away_color"), g.get("venue"), g.get("venue_city"),
                 g.get("tv_national"), g.get("broadcast_json"), h, now, row["id"]),
            )
            updated += 1
        else:
            skipped += 1

    conn.execute(
        """INSERT INTO sports_sync_log
           (provider, league, run_utc, games_seen, games_inserted,
            games_updated, games_skipped, status, message)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (provider, league, now, len(games), inserted, updated, skipped, "ok", None),
    )
    conn.commit()
    return {"seen": len(games), "inserted": inserted, "updated": updated, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize(row):
    g = dict(row)
    bc = g.get("broadcast_json")
    g["broadcast"] = json.loads(bc) if bc else {"tv": [], "directv": [], "streaming": []}
    g.pop("broadcast_json", None)
    g["is_live"] = g["status"] in LIVE_STATES
    g["is_final"] = g["status"] == STATUS_FINAL
    g.update(central_fields(g["start_utc"]))
    return g


def _rows(conn, sql, params=()):
    return [serialize(r) for r in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------- #
# Category queries
# --------------------------------------------------------------------------- #
# "Today" must be filtered on the Houston calendar date, which the DB stores in
# UTC. We can't compare dates in SQL safely across the offset, so we bound the
# query by the UTC instants of Houston midnight-to-midnight, then trust the
# index on start_utc.
def _central_day_bounds(day=None):
    day = day or central_today()
    start_local = datetime(day.year, day.month, day.day, 0, 0, tzinfo=CENTRAL)
    end_local = start_local + timedelta(days=1)
    return (start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def get_today(conn, league=None):
    lo, hi = _central_day_bounds()
    sql = "SELECT * FROM sports_games WHERE start_utc >= ? AND start_utc < ?"
    params = [lo, hi]
    if league:
        sql += " AND league = ?"; params.append(league)
    sql += " ORDER BY start_utc ASC"
    return _rows(conn, sql, params)


def get_live(conn, league=None):
    ph = ",".join("?" * len(LIVE_STATES))
    sql = f"SELECT * FROM sports_games WHERE status IN ({ph})"
    params = list(LIVE_STATES)
    if league:
        sql += " AND league = ?"; params.append(league)
    sql += " ORDER BY start_utc ASC"
    return _rows(conn, sql, params)


def get_upcoming(conn, days=8, league=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    horizon = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ph = ",".join("?" * len(UPCOMING_STATES))
    sql = (f"SELECT * FROM sports_games WHERE status IN ({ph}) "
           "AND start_utc > ? AND start_utc <= ?")
    params = list(UPCOMING_STATES) + [now, horizon]
    if league:
        sql += " AND league = ?"; params.append(league)
    sql += " ORDER BY start_utc ASC"
    return _rows(conn, sql, params)


def get_completed(conn, limit=50, league=None):
    """All final games, newest first (includes today's finals)."""
    sql = "SELECT * FROM sports_games WHERE status = ?"
    params = [STATUS_FINAL]
    if league:
        sql += " AND league = ?"; params.append(league)
    sql += " ORDER BY start_utc DESC LIMIT ?"; params.append(limit)
    return _rows(conn, sql, params)


def get_previous(conn, limit=50, offset=0, league=None):
    """Final games from before today (Houston). The archive that stays visible."""
    lo, _ = _central_day_bounds()
    sql = "SELECT * FROM sports_games WHERE status = ? AND start_utc < ?"
    params = [STATUS_FINAL, lo]
    if league:
        sql += " AND league = ?"; params.append(league)
    sql += " ORDER BY start_utc DESC LIMIT ? OFFSET ?"; params += [limit, offset]
    return _rows(conn, sql, params)


def get_favorites(conn, user_id, league=None):
    favs = conn.execute(
        "SELECT scope, ref FROM sports_favorites WHERE user_id = ?", (user_id,)
    ).fetchall()
    if not favs:
        return []
    game_ids = [f["ref"] for f in favs if f["scope"] == "game"]
    teams = [f["ref"] for f in favs if f["scope"] == "team"]
    leagues = [f["ref"] for f in favs if f["scope"] == "league"]

    clauses, params = [], []
    if game_ids:
        clauses.append(f"provider_game_id IN ({','.join('?'*len(game_ids))})")
        params += game_ids
    if teams:
        ph = ",".join("?" * len(teams))
        clauses.append(f"(home_team IN ({ph}) OR away_team IN ({ph}) "
                       f"OR home_abbr IN ({ph}) OR away_abbr IN ({ph}))")
        params += teams * 4
    if leagues:
        clauses.append(f"league IN ({','.join('?'*len(leagues))})")
        params += leagues
    if not clauses:
        return []
    sql = f"SELECT * FROM sports_games WHERE ({' OR '.join(clauses)})"
    if league:
        sql += " AND league = ?"; params.append(league)
    sql += " ORDER BY start_utc DESC LIMIT 100"
    return _rows(conn, sql, params)


def search_games(conn, q, limit=50):
    like = f"%{q}%"
    sql = ("SELECT * FROM sports_games WHERE "
           "home_team LIKE ? OR away_team LIKE ? OR home_abbr LIKE ? OR "
           "away_abbr LIKE ? OR league LIKE ? OR venue LIKE ? OR "
           "venue_city LIKE ? OR tv_national LIKE ? "
           "ORDER BY start_utc DESC LIMIT ?")
    return _rows(conn, sql, [like] * 8 + [limit])


# --------------------------------------------------------------------------- #
# Favorites mutation
# --------------------------------------------------------------------------- #
def add_favorite(conn, user_id, scope, ref):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO sports_favorites (user_id, scope, ref, created_utc)
           VALUES (?,?,?,?) ON CONFLICT(user_id, scope, ref) DO NOTHING""",
        (user_id, scope, ref, now),
    )
    conn.commit()


def remove_favorite(conn, user_id, scope, ref):
    conn.execute(
        "DELETE FROM sports_favorites WHERE user_id=? AND scope=? AND ref=?",
        (user_id, scope, ref),
    )
    conn.commit()


def list_favorites(conn, user_id):
    return [dict(r) for r in conn.execute(
        "SELECT scope, ref, created_utc FROM sports_favorites WHERE user_id=?",
        (user_id,)).fetchall()]


def counts(conn, user_id=None, league=None):
    """Tab badge counts."""
    return {
        "today": len(get_today(conn, league)),
        "live": len(get_live(conn, league)),
        "upcoming": len(get_upcoming(conn, league=league)),
        "completed": len(get_completed(conn, league=league)),
        "previous": len(get_previous(conn, league=league)),
        "favorites": len(get_favorites(conn, user_id, league)) if user_id else 0,
    }
