"""
live_feed.py — live "what's on" sports aggregator.

Pulls real games across ALL major sports from ESPN's public scoreboard API
(site.api.espn.com — free, no key) for a date window, normalizes each event
into the shape the Sports Board front-end expects, and caches the result in
process for a short TTL so the page is always current without hammering ESPN.

This replaces the board's SAMPLE DATA with live games (Switzerland v Qatar,
the RBC Canadian Open, every MLB game, etc.). Broadcast network names are
normalized to the canonical keys the board's DirecTV / Xfinity channel map
uses (sports_broadcast.py / the client maps), so the Houston "put it on
channel X" tiles keep working on live data.

No DB, no cron: one cached HTTP sweep per ~60s. The page renders instantly
from cache; the sweep refreshes in the background.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_UA = {"User-Agent": "Mozilla/5.0 (CenasKitchen SportsBoard)"}

# --- League catalog -----------------------------------------------------------
# (sport, espn_league_code, display_name). Off-season leagues simply return 0
# events and are skipped cheaply. Broad on purpose — "a full search for all
# sports". Add a row to widen coverage; never hardcode game data.
LEAGUES = [
    # Soccer — internationals first (World Cup window), then club
    ("soccer", "fifa.world",            "FIFA World Cup"),
    ("soccer", "fifa.cwc",              "FIFA Club World Cup"),
    ("soccer", "fifa.friendly",         "Intl Friendly"),
    ("soccer", "fifa.friendly.w",       "Intl Friendly (W)"),
    ("soccer", "fifa.worldq.uefa",      "WC Qualifying — UEFA"),
    ("soccer", "fifa.worldq.conmebol",  "WC Qualifying — CONMEBOL"),
    ("soccer", "fifa.worldq.concacaf",  "WC Qualifying — CONCACAF"),
    ("soccer", "fifa.worldq.afc",       "WC Qualifying — AFC"),
    ("soccer", "fifa.worldq.caf",       "WC Qualifying — CAF"),
    ("soccer", "uefa.nations",          "UEFA Nations League"),
    ("soccer", "uefa.euro",             "UEFA Euro"),
    ("soccer", "conmebol.america",      "Copa América"),
    ("soccer", "concacaf.gold",         "Gold Cup"),
    ("soccer", "uefa.champions",        "UEFA Champions League"),
    ("soccer", "uefa.europa",           "UEFA Europa League"),
    ("soccer", "eng.1",                 "Premier League"),
    ("soccer", "esp.1",                 "La Liga"),
    ("soccer", "ger.1",                 "Bundesliga"),
    ("soccer", "ita.1",                 "Serie A"),
    ("soccer", "fra.1",                 "Ligue 1"),
    ("soccer", "usa.1",                 "MLS"),
    ("soccer", "mex.1",                 "Liga MX"),
    ("soccer", "club.friendly",         "Club Friendly"),
    # Baseball
    ("baseball", "mlb",                 "MLB"),
    ("baseball", "college-baseball",    "College Baseball"),
    # Basketball
    ("basketball", "nba",               "NBA"),
    ("basketball", "wnba",              "WNBA"),
    # Hockey
    ("hockey", "nhl",                   "NHL"),
    # Football
    ("football", "nfl",                 "NFL"),
    ("football", "college-football",    "College Football"),
    # Golf (tournament events)
    ("golf", "pga",                     "PGA Tour"),
    ("golf", "lpga",                    "LPGA"),
    ("golf", "champions-tour",          "PGA Tour Champions"),
    ("golf", "eur",                     "DP World Tour"),
    ("golf", "liv",                     "LIV Golf"),
    # Tennis (match events)
    ("tennis", "atp",                   "ATP"),
    ("tennis", "wta",                   "WTA"),
    # Combat / motorsport (event cards)
    ("mma", "ufc",                      "UFC"),
    ("racing", "f1",                    "Formula 1"),
]

SPORT_OF_LEAGUE = {code: sport for (sport, code, _name) in LEAGUES}

# --- Broadcast-network normalization -----------------------------------------
# ESPN short names -> the canonical keys the DirecTV/Xfinity channel map uses.
# Unknown names (out-of-market RSNs like "Twins.TV", "YES", "MASN") pass
# through unchanged -> they resolve to "Not available" on Houston DirecTV/
# Xfinity, which is correct (a Houston TV can't tune another market's RSN).
_NET_ALIASES = {
    "abc": "ABC", "cbs": "CBS", "nbc": "NBC", "fox": "FOX",
    "espn": "ESPN", "espn2": "ESPN2", "espnu": "ESPNU", "espnews": "ESPNEWS",
    "espn+": "ESPN+", "espn plus": "ESPN+", "espn unlimited": "ESPN+",
    "espn unlmtd": "ESPN+", "espn deportes": "ESPN Deportes",
    "fs1": "FS1", "fs2": "FS2", "fox deportes": "FOX Deportes", "fox one": "FOX One",
    "tnt": "TNT", "tbs": "TBS", "trutv": "truTV", "max": "Max",
    "tele": "Telemundo", "telemundo": "Telemundo", "universo": "Universo",
    "unimas": "UniMás", "univision": "Univision",
    "peacock": "Peacock", "paramount+": "Paramount+", "paramount plus": "Paramount+",
    "netflix": "Netflix", "apple tv+": "Apple TV+", "apple tv": "Apple TV+",
    "prime video": "Amazon Prime Video", "amazon prime video": "Amazon Prime Video",
    "golf chnl": "Golf Channel", "golf channel": "Golf Channel",
    "tennis channel": "Tennis Channel", "tennis": "Tennis Channel",
    "mlb.tv": "MLB.TV", "mlb network": "MLB Network", "mlbn": "MLB Network",
    "nba tv": "NBA TV", "nbatv": "NBA TV", "nhl network": "NHL Network",
    "nhln": "NHL Network", "nfl network": "NFL Network", "nfln": "NFL Network",
    "nfl redzone": "NFL RedZone",
    "secn": "SEC Network", "sec network": "SEC Network",
    "accn": "ACC Network", "acc network": "ACC Network",
    "btn": "Big Ten Network", "big ten network": "Big Ten Network",
    "cbssn": "CBS Sports Network", "cbs sports network": "CBS Sports Network",
    "cbs sports net": "CBS Sports Network",
    "usa": "USA Network", "usa network": "USA Network", "tubi": "Tubi",
    "space city home network": "Space City Home Network", "schn": "Space City Home Network",
}

# Nice short names where ESPN's league name is long.
_LEAGUE_DISPLAY = {
    "Major League Baseball": "MLB",
    "National Basketball Association": "NBA",
    "National Hockey League": "NHL",
}

# Map ESPN status.type.name -> the board's normalized status vocabulary.
_STATUS_MAP = {
    "STATUS_SCHEDULED": "scheduled", "STATUS_PRE": "scheduled",
    "STATUS_IN_PROGRESS": "in_progress", "STATUS_FIRST_HALF": "in_progress",
    "STATUS_SECOND_HALF": "in_progress", "STATUS_END_PERIOD": "in_progress",
    "STATUS_END_OF_PERIOD": "in_progress", "STATUS_OVERTIME": "in_progress",
    "STATUS_HALFTIME": "halftime",
    "STATUS_DELAYED": "delayed", "STATUS_RAIN_DELAY": "delayed",
    "STATUS_FINAL": "final", "STATUS_FULL_TIME": "final",
    "STATUS_FINAL_PEN": "final", "STATUS_FINAL_AET": "final",
    "STATUS_POSTPONED": "postponed", "STATUS_CANCELED": "canceled",
    "STATUS_ABANDONED": "canceled", "STATUS_FORFEIT": "final",
}

_LIVE = {"in_progress", "halftime", "delayed"}


def _norm_net(raw):
    if not raw:
        return None
    return _NET_ALIASES.get(raw.strip().lower(), raw.strip())


def _nets_for(comp):
    """Ordered, de-duplicated canonical network names for a competition,
    national-market broadcasts first (most relevant to a Houston bar)."""
    out, seen = [], set()

    def add(name):
        n = _norm_net(name)
        if n and n not in seen:
            seen.add(n)
            out.append(n)

    # broadcasts: [{market, names:[...]}] — national first
    bcs = comp.get("broadcasts") or []
    for market in ("national", "home", "away", None):
        for b in bcs:
            if market is None or b.get("market") == market:
                for nm in (b.get("names") or []):
                    add(nm)
    # geoBroadcasts fallback
    for gb in comp.get("geoBroadcasts") or []:
        media = gb.get("media") or {}
        add(media.get("shortName") or media.get("callSign"))
    return out


def _team(competitor):
    # Team sports use competitor.team; individual sports (tennis) use
    # competitor.athlete. Fall back across both.
    t = competitor.get("team") or {}
    ath = competitor.get("athlete") or {}
    color = (t.get("color") or "").strip()
    if not color or color in ("000000", "ffffff"):
        color = (t.get("alternateColor") or "").strip() or "64748b"
    score = competitor.get("score")
    try:
        score = int(score) if score not in (None, "", " ") else None
    except (TypeError, ValueError):
        score = None
    name = (t.get("shortDisplayName") or t.get("displayName") or t.get("name")
            or ath.get("shortName") or ath.get("displayName") or "TBD")
    abbr = (t.get("abbreviation") or ath.get("shortName") or name[:3]).upper()
    return {
        "name": name,
        "abbr": abbr[:4],
        "color": "#" + color.lstrip("#"),
        "score": score,
    }


def _normalize_event(ev, league_name, sport):
    try:
        comp = (ev.get("competitions") or [{}])[0]
        stype = (ev.get("status") or {}).get("type") or {}
        status = _STATUS_MAP.get(stype.get("name", ""), (stype.get("state") or "scheduled"))
        if status in ("pre",):
            status = "scheduled"
        if status in ("in",):
            status = "in_progress"
        if status in ("post",):
            status = "final"
        if status in ("canceled", "postponed"):
            return None  # not watchable — drop from "what's on"

        venue = comp.get("venue") or {}
        addr = venue.get("address") or {}
        city = addr.get("city")
        if city and addr.get("state"):
            city = f"{city}, {addr['state']}"
        elif city and addr.get("country") and addr["country"] != "USA":
            city = f"{city}, {addr['country']}"

        competitors = comp.get("competitors") or []
        nets = _nets_for(comp)
        detail = stype.get("shortDetail") or stype.get("detail") or ""
        note = ""
        notes = comp.get("notes") or []
        if notes:
            note = notes[0].get("headline", "") or ""

        base = {
            "id": "espn-" + str(ev.get("id")),
            "league": _LEAGUE_DISPLAY.get(league_name, league_name),
            "sport": sport,
            "round": note,
            "status": status,
            "detail": detail,
            "start_utc": ev.get("date"),
            "venue": venue.get("fullName") or "",
            "city": city or "",
            "nets": nets,
        }

        if len(competitors) == 2:
            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[-1])
            base["home"] = _team(home)
            base["away"] = _team(away)
            base["single"] = False
        else:
            # Tournament / event card (golf, racing, fight night): one title row.
            base["home"] = {
                "name": ev.get("shortName") or ev.get("name") or league_name,
                "abbr": sport[:3].upper(), "color": "#3a4757", "score": None,
            }
            base["away"] = None
            base["single"] = True
            if not base["round"] and detail:
                base["round"] = detail
        return base
    except Exception as e:  # never let one bad event kill the sweep
        log.debug("sports live: skip malformed event: %s", e)
        return None


def _fetch_league(sport, code, name, date_range):
    url = f"{_BASE}/{sport}/{code}/scoreboard"
    if date_range:
        url += f"?dates={date_range}"
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.load(r)
    except Exception as e:
        log.debug("sports live: fetch failed %s/%s: %s", sport, code, e)
        return []
    league_name = name  # prefer our clean catalog name over ESPN's verbose one
    out = []
    for ev in data.get("events") or []:
        g = _normalize_event(ev, league_name, sport)
        if g:
            out.append(g)
    return out


def _sweep(days_back=2, days_fwd=4, max_games=600):
    """One concurrent sweep across the whole catalog for the date window."""
    today = datetime.now(timezone.utc).date()
    rng = (f"{(today - timedelta(days=days_back)):%Y%m%d}-"
           f"{(today + timedelta(days=days_fwd)):%Y%m%d}")
    games = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(_fetch_league, s, c, n, rng): c for (s, c, n) in LEAGUES}
        for f in as_completed(futs):
            try:
                games.extend(f.result())
            except Exception as e:
                log.debug("sports live: league future failed %s: %s", futs[f], e)
    # de-dup by id (a game can appear under more than one league code)
    seen, deduped = set(), []
    for g in games:
        if g["id"] in seen:
            continue
        seen.add(g["id"])
        deduped.append(g)
    deduped.sort(key=lambda g: (g.get("start_utc") or ""))
    if len(deduped) > max_games:
        deduped = deduped[:max_games]
    return deduped


# --- TTL cache ----------------------------------------------------------------
_CACHE = {"at": 0.0, "games": [], "leagues": 0}
_LOCK = threading.Lock()
_TTL = 60.0  # seconds


def get_live_games(ttl=_TTL, force=False):
    """Return (games, meta). Cached for `ttl` seconds. Thread-safe; a stale
    entry is refreshed by the first caller, others get the previous list while
    it warms (never blocks more than one sweep at a time)."""
    import time
    now = time.time()
    with _LOCK:
        fresh = (not force) and (now - _CACHE["at"] < ttl) and _CACHE["games"]
        if fresh:
            return _CACHE["games"], {"cached": True, "age": round(now - _CACHE["at"], 1),
                                     "count": len(_CACHE["games"])}
    # refresh outside the lock so concurrent readers aren't blocked
    games = _sweep()
    with _LOCK:
        if games:                      # keep last-good on a fully-failed sweep
            _CACHE["at"] = now
            _CACHE["games"] = games
        served = _CACHE["games"]
    return served, {"cached": False, "age": 0.0, "count": len(served)}


if __name__ == "__main__":
    import sys
    games, meta = get_live_games(force=True)
    print(f"swept {meta['count']} games across {len(LEAGUES)} leagues")
    by_status = {}
    for g in games:
        by_status[g["status"]] = by_status.get(g["status"], 0) + 1
    print("by status:", by_status)
    print("\nsample (first 25):")
    for g in games[:25]:
        a = g["away"]["name"] + " at " if g["away"] else ""
        chans = ",".join(g["nets"][:3]) or "-"
        print(f"  {g['league'][:20]:20} | {g['status']:11} {g['detail'][:14]:14} | "
              f"{a}{g['home']['name']}"[:42].ljust(42) + f" | {chans}")
    # show the user's examples if present
    print("\nlooking for the asked-about games:")
    for needle in ("Switzerland", "Qatar", "Haiti", "Scotland", "Canadian Open"):
        hits = [g for g in games if needle.lower() in json.dumps(g).lower()]
        print(f"  {needle:16}: {len(hits)} match(es)" +
              (f" -> {hits[0]['home']['name']} ({hits[0]['nets'][:2]})" if hits else ""))
