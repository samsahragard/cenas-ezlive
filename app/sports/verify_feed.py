"""
verify_feed.py — keep the Sports Board scores up to date by cross-checking
ESPN against each league's OWN official, free API.

ESPN is the comprehensive finder/feed. This layer adds a second, authoritative
source per league and reconciles: for every MLB / WNBA / NBA / NHL game we can
match, we adopt the official score + game-state (the league's own data is the
source of truth) and flag the game `verified` with `verify_src`. Soccer / golf /
tennis / etc. have no comparable free official API, so they stay ESPN-only.

All sources are free and keyless:
  - MLB : statsapi.mlb.com           (date-windowed, authoritative)
  - WNBA: cdn.wnba.com  liveData     (today's slate)
  - NBA : cdn.nba.com   liveData     (today's slate; needs a Referer header)
  - NHL : api-web.nhle.com           (date-windowed)

Everything here is best-effort: any source that errors is skipped and the ESPN
value stands. Never raises into the sweep.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

OFFICIAL_LEAGUES = {"MLB", "WNBA", "NBA", "NHL"}
_TTL = 60.0
_CACHE = {"at": 0.0, "games": []}
_LOCK = threading.Lock()

_STOP = {"the", "of", "fc", "sc"}


def _toks(name):
    """Lowercase alphanumeric tokens of a team name, for subset matching
    (ESPN's short name tokens ⊆ the official full-name tokens)."""
    return {t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if t and t not in _STOP}


def _get(url, headers=None, timeout=10):
    h = {"User-Agent": "Mozilla/5.0 (CenasKitchen SportsBoard)"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _date_window():
    today = datetime.now(timezone.utc).date()
    return [today + timedelta(days=d) for d in range(-2, 5)]


# --- per-league fetch -> common official-game shape ---------------------------
def _mk(league, src, start_utc, away_name, away_score, home_name, home_score, status, detail):
    if status == "scheduled":
        away_score = home_score = None
    return {
        "league": league, "src": src,
        "date": (start_utc or "")[:10],
        "start_utc": start_utc,
        "away_tokens": _toks(away_name), "home_tokens": _toks(home_name),
        "away_score": away_score, "home_score": home_score,
        "status": status, "detail": detail or "",
    }


def _mlb(dates):
    s, e = dates[0].isoformat(), dates[-1].isoformat()
    out = []
    try:
        d = _get(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={s}&endDate={e}")
    except Exception as ex:
        log.debug("verify mlb: %s", ex); return out
    smap = {"Preview": "scheduled", "Live": "in_progress", "Final": "final"}
    for day in d.get("dates", []):
        for g in day.get("games", []):
            st = smap.get((g.get("status") or {}).get("abstractGameState"), "scheduled")
            aw, hm = g["teams"]["away"], g["teams"]["home"]
            out.append(_mk("MLB", "MLB.com", g.get("gameDate"),
                           aw["team"]["name"], aw.get("score"),
                           hm["team"]["name"], hm.get("score"),
                           st, (g.get("status") or {}).get("detailedState", "")))
    return out


def _nhl(dates):
    out = []
    smap = {"FUT": "scheduled", "PRE": "scheduled", "LIVE": "in_progress",
            "CRIT": "in_progress", "FINAL": "final", "OFF": "final"}
    for dt in dates:
        try:
            d = _get(f"https://api-web.nhle.com/v1/score/{dt.isoformat()}")
        except Exception as ex:
            log.debug("verify nhl %s: %s", dt, ex); continue
        for g in d.get("games", []):
            st = smap.get(g.get("gameState", ""), "scheduled")
            aw, hm = g.get("awayTeam", {}), g.get("homeTeam", {})
            nm = lambda t: " ".join(filter(None, [t.get("placeName", {}).get("default", ""),
                                                  t.get("commonName", {}).get("default", ""),
                                                  t.get("abbrev", "")]))
            out.append(_mk("NHL", "NHL.com", g.get("startTimeUTC"),
                           nm(aw), aw.get("score"), nm(hm), hm.get("score"),
                           st, ""))
    return out


def _cdn_nba_wnba(league, url, referer):
    out = []
    smap = {1: "scheduled", 2: "in_progress", 3: "final"}
    try:
        d = _get(url, headers={"Referer": referer})
    except Exception as ex:
        log.debug("verify %s: %s", league, ex); return out
    for g in d.get("scoreboard", {}).get("games", []):
        st = smap.get(g.get("gameStatus"), "scheduled")
        aw, hm = g.get("awayTeam", {}), g.get("homeTeam", {})
        nm = lambda t: " ".join(filter(None, [t.get("teamCity", ""), t.get("teamName", ""), t.get("teamTricode", "")]))
        out.append(_mk(league, f"{league}.com", g.get("gameTimeUTC") or g.get("gameEt"),
                       nm(aw), aw.get("score"), nm(hm), hm.get("score"),
                       st, (g.get("gameStatusText") or "").strip()))
    return out


def _fetch_all_official():
    dates = _date_window()
    jobs = [
        ("mlb", lambda: _mlb(dates)),
        ("nhl", lambda: _nhl(dates)),
        ("nba", lambda: _cdn_nba_wnba("NBA", "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json", "https://www.nba.com/")),
        ("wnba", lambda: _cdn_nba_wnba("WNBA", "https://cdn.wnba.com/static/json/liveData/scoreboard/todaysScoreboard_10.json", "https://www.wnba.com/")),
    ]
    out = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(fn): name for name, fn in jobs}
        for f in as_completed(futs):
            try:
                out.extend(f.result())
            except Exception as ex2:
                log.debug("verify %s future: %s", futs[f], ex2)
    return out


def _official(ttl=_TTL):
    now = time.time()
    with _LOCK:
        if now - _CACHE["at"] < ttl and _CACHE["games"]:
            return _CACHE["games"]
    games = _fetch_all_official()
    with _LOCK:
        if games:
            _CACHE["at"] = now
            _CACHE["games"] = games
        return _CACHE["games"]


def _best_match(g, candidates):
    home_t = _toks(g["home"]["name"])
    away_t = _toks(g["away"]["name"])
    gd = (g.get("start_utc") or "")[:10]
    hits = []
    for c in candidates:
        if not (away_t and home_t):
            continue
        if away_t <= c["away_tokens"] and home_t <= c["home_tokens"]:
            # date must agree when both have one (today-only sources have a date too)
            if gd and c["date"] and gd != c["date"]:
                continue
            hits.append(c)
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    # disambiguate doubleheaders/series by closest start time
    def closeness(c):
        try:
            return abs((datetime.fromisoformat((c["start_utc"] or "").replace("Z", "+00:00"))
                        - datetime.fromisoformat((g["start_utc"] or "").replace("Z", "+00:00"))).total_seconds())
        except Exception:
            return 1e12
    return min(hits, key=closeness)


def reconcile(games):
    """Mutate `games` in place: for each MLB/WNBA/NBA/NHL game we can match to
    its league's official source, adopt the authoritative score + state and tag
    it verified. Returns the number of games verified. Never raises."""
    try:
        official = _official()
    except Exception as ex:
        log.debug("verify: official fetch failed: %s", ex)
        return 0
    by_league = {}
    for c in official:
        by_league.setdefault(c["league"], []).append(c)
    n = 0
    for g in games:
        if g.get("single") or not g.get("away"):
            continue
        if g.get("league") not in OFFICIAL_LEAGUES:
            continue
        m = _best_match(g, by_league.get(g["league"], []))
        if not m:
            continue
        if m["home_score"] is not None:
            g["home"]["score"] = m["home_score"]
        if m["away_score"] is not None:
            g["away"]["score"] = m["away_score"]
        if m["status"] != g["status"]:
            g["status"] = m["status"]
            if m["detail"]:
                g["detail"] = m["detail"]
        g["verified"] = True
        g["verify_src"] = m["src"]
        n += 1
    return n


if __name__ == "__main__":
    off = _official()
    print(f"official games pulled: {len(off)}")
    from collections import Counter
    print("by league:", dict(Counter(c["league"] for c in off)))
    for c in off[:6]:
        print(f"  {c['league']:4} {('/'.join(sorted(c['away_tokens']))):24} @ "
              f"{('/'.join(sorted(c['home_tokens']))):24} {c['away_score']}-{c['home_score']} [{c['status']}] {c['detail']}")
