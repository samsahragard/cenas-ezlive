"""
sports_provider.py — pluggable data sources.

A provider turns an external schedule/scoreboard feed into the normalized game
dicts that sports_core.upsert_games() expects. Swap providers without touching
the engine, the routes, or the UI.

  * EspnProvider     — reference implementation against ESPN's public scoreboard
                       endpoints. Zero cost, no key. Endpoints are undocumented,
                       so treat them as best-effort and pin a paid provider
                       (SportRadar / API-Sports) for production reliability.
  * FixtureProvider  — returns a fixed list of games for offline tests / demos.

Note: the live HTTP path needs outbound network access to site.api.espn.com
(open on Render; not reachable from the sandboxed build environment), so the
fetch path is exercised in HIS environment and the transform is unit-tested
here with captured fixtures.
"""

import json
import urllib.request
from datetime import datetime, timezone

from sports_core import (
    STATUS_SCHEDULED, STATUS_PRE, STATUS_LIVE, STATUS_HALFTIME,
    STATUS_DELAYED, STATUS_POSTPONED, STATUS_FINAL, STATUS_CANCELED,
)
from sports_broadcast import resolve_broadcast


class SportsProvider:
    name = "base"

    def fetch(self, league_key):
        """Return a list of normalized game dicts. Override in subclasses."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# ESPN reference provider
# --------------------------------------------------------------------------- #
# ESPN status.type.state -> our normalized status
_ESPN_STATE = {
    "pre": STATUS_SCHEDULED,
    "in": STATUS_LIVE,
    "post": STATUS_FINAL,
}
# Map our league keys to ESPN (sport, league) path segments.
ESPN_LEAGUES = {
    "NFL": ("football", "nfl"),
    "NCAAF": ("football", "college-football"),
    "NBA": ("basketball", "nba"),
    "WNBA": ("basketball", "wnba"),
    "NCAAM": ("basketball", "mens-college-basketball"),
    "MLB": ("baseball", "mlb"),
    "NHL": ("hockey", "nhl"),
    "FIFA World Cup": ("soccer", "fifa.world"),
    "EPL": ("soccer", "eng.1"),
    "MLS": ("soccer", "usa.1"),
}
_SPORT_LABEL = {"football": "football", "basketball": "basketball",
                "baseball": "baseball", "hockey": "hockey", "soccer": "soccer"}


class EspnProvider(SportsProvider):
    name = "espn"
    BASE = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard"

    def __init__(self, timeout=12):
        self.timeout = timeout

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "CenasSportsTracker/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def fetch(self, league_key):
        if league_key not in ESPN_LEAGUES:
            raise ValueError(f"Unknown league for ESPN provider: {league_key}")
        sport, league = ESPN_LEAGUES[league_key]
        data = self._get(self.BASE.format(sport=sport, league=league))
        return self.transform(data, league_key, sport)

    # transform() is separated so it can be unit-tested on captured JSON.
    def transform(self, data, league_key, sport):
        out = []
        for ev in data.get("events", []):
            try:
                out.append(self._event_to_game(ev, league_key, sport))
            except Exception:
                continue  # never let one malformed event break a whole sync
        return out

    def _event_to_game(self, ev, league_key, sport):
        comp = ev["competitions"][0]
        competitors = comp["competitors"]
        home = next(c for c in competitors if c.get("homeAway") == "home")
        away = next(c for c in competitors if c.get("homeAway") == "away")

        st = ev.get("status", {}).get("type", {})
        status = _ESPN_STATE.get(st.get("state"), STATUS_SCHEDULED)
        if st.get("name") == "STATUS_HALFTIME":
            status = STATUS_HALFTIME
        elif st.get("name") in ("STATUS_POSTPONED",):
            status = STATUS_POSTPONED
        elif st.get("name") in ("STATUS_CANCELED",):
            status = STATUS_CANCELED
        status_detail = st.get("shortDetail") or st.get("detail")

        venue = comp.get("venue", {})
        addr = venue.get("address", {})

        networks = []
        for b in comp.get("broadcasts", []):
            networks.extend(b.get("names", []))
        for g in comp.get("geoBroadcasts", []):
            nm = g.get("media", {}).get("shortName")
            if nm:
                networks.append(nm)

        broadcast = resolve_broadcast(league_key, networks, sport=sport)

        def score(c):
            v = c.get("score")
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        def color(c):
            col = c.get("team", {}).get("color")
            return f"#{col}" if col and not col.startswith("#") else col

        return {
            "provider_game_id": str(ev["id"]),
            "sport": _SPORT_LABEL.get(sport, sport),
            "league": league_key,
            "season": str(ev.get("season", {}).get("year", "")) or None,
            "status": status,
            "status_detail": status_detail,
            "start_utc": self._iso_z(ev["date"]),
            "home_team": home["team"].get("displayName"),
            "home_abbr": home["team"].get("abbreviation"),
            "home_score": score(home),
            "home_color": color(home),
            "away_team": away["team"].get("displayName"),
            "away_abbr": away["team"].get("abbreviation"),
            "away_score": score(away),
            "away_color": color(away),
            "venue": venue.get("fullName"),
            "venue_city": ", ".join(p for p in [addr.get("city"), addr.get("state")] if p) or None,
            "tv_national": ", ".join(broadcast["tv"]) or None,
            "broadcast_json": broadcast,
        }

    @staticmethod
    def _iso_z(espn_date):
        # ESPN returns e.g. "2026-06-13T19:00Z"; normalize to seconds + Z.
        s = espn_date.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Fixture provider (offline tests / demos)
# --------------------------------------------------------------------------- #
class FixtureProvider(SportsProvider):
    name = "fixture"

    def __init__(self, games):
        self._games = games

    def fetch(self, league_key=None):
        return [dict(g) for g in self._games]
