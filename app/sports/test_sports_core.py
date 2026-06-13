"""
test_sports_core.py — offline verification of the engine.

Builds realistic mid-June-2026 fixtures relative to the current Houston time,
then asserts: no duplicates, idempotent re-sync, correct update counting,
correct Today/Live/Upcoming/Completed/Previous bucketing across the UTC->Central
boundary, favorites union, and search. Exits non-zero on any failure.
"""

from datetime import timedelta

import sports_core as core
from sports_broadcast import resolve_broadcast

CENTRAL = core.CENTRAL
PASS, FAIL = [], []


def check(label, cond):
    (PASS if cond else FAIL).append(label)
    print(("  PASS " if cond else "  FAIL ") + label)


def utc_from_central(dt_central):
    from datetime import timezone
    return dt_central.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def game(gid, league, sport, status, start_central, home, hab=None, hs=None,
         away=None, aab=None, as_=None, networks=None, venue=None, city=None,
         detail=None):
    return {
        "provider_game_id": gid, "league": league, "sport": sport,
        "status": status, "status_detail": detail,
        "start_utc": utc_from_central(start_central),
        "home_team": home, "home_abbr": hab, "home_score": hs, "home_color": "#0E3386",
        "away_team": away, "away_abbr": aab, "away_score": as_, "away_color": "#BA0021",
        "venue": venue, "venue_city": city,
        "tv_national": ", ".join((networks or [])) or None,
        "broadcast_json": resolve_broadcast(league, networks or []),
    }


def build_fixtures(now):
    today = now.date()
    midday = now.replace(hour=12, minute=0, second=0, microsecond=0)
    yday_eve = (now - timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
    tom_eve = (now + timedelta(days=1)).replace(hour=18, minute=10, second=0, microsecond=0)
    later_today = now.replace(hour=23, minute=0, second=0, microsecond=0) \
        if now.hour < 21 else now + timedelta(hours=2)

    return [
        # World Cup match in Houston earlier today, FINAL -> Today + Completed
        game("wc-hou-01", "FIFA World Cup", "soccer", core.STATUS_FINAL,
             midday, "Mexico", "MEX", 2, "South Korea", "KOR", 1,
             ["FOX", "Telemundo"], "NRG Stadium", "Houston, TX", "Full Time"),
        # Astros game tonight, LIVE -> Today + Live
        game("mlb-hou-01", "MLB", "baseball", core.STATUS_LIVE,
             later_today, "Houston Astros", "HOU", 3, "Seattle Mariners", "SEA", 2,
             ["Space City Home Network"], "Daikin Park", "Houston, TX", "Top 7th"),
        # NHL game live now -> Live (started before now)
        game("nhl-01", "NHL", "hockey", core.STATUS_LIVE,
             now - timedelta(hours=1), "Dallas Stars", "DAL", 1,
             "Edmonton Oilers", "EDM", 1, ["TNT"], "American Airlines Center",
             "Dallas, TX", "2nd Period"),
        # NBA Finals last night, FINAL -> Previous + Completed (not Today)
        game("nba-fin-01", "NBA", "basketball", core.STATUS_FINAL,
             yday_eve, "Oklahoma City Thunder", "OKC", 112,
             "Indiana Pacers", "IND", 105, ["ABC"], "Paycom Center",
             "Oklahoma City, OK", "Final"),
        # MLB game tomorrow, SCHEDULED -> Upcoming
        game("mlb-hou-02", "MLB", "baseball", core.STATUS_SCHEDULED,
             tom_eve, "Houston Astros", "HOU", None, "Texas Rangers", "TEX", None,
             ["Space City Home Network"], "Daikin Park", "Houston, TX"),
        # World Cup match tonight, SCHEDULED -> Today + Upcoming
        game("wc-02", "FIFA World Cup", "soccer", core.STATUS_SCHEDULED,
             later_today, "Brazil", "BRA", None, "Morocco", "MAR", None,
             ["FS1", "Telemundo"], "Mercedes-Benz Stadium", "Atlanta, GA"),
    ]


def main():
    now = core.central_now()
    print(f"Houston now: {now.strftime('%a %Y-%m-%d %I:%M %p %Z')}\n")

    conn = core.connect(":memory:")
    core.init_db(conn, "sports_schema.sql")
    fx = build_fixtures(now)

    print("First sync:")
    s1 = core.upsert_games(conn, fx, provider="fixture")
    print("   ", s1)
    check("first sync inserts all 6", s1["inserted"] == 6 and s1["updated"] == 0)

    print("Re-sync identical (idempotency / dedup):")
    s2 = core.upsert_games(conn, fx, provider="fixture")
    print("   ", s2)
    check("re-sync inserts 0, skips 6", s2["inserted"] == 0 and s2["skipped"] == 6)
    total = conn.execute("SELECT COUNT(*) AS n FROM sports_games").fetchone()["n"]
    check("no duplicate rows after re-sync (still 6)", total == 6)

    print("Mutate two games (score + status transition):")
    fx2 = [dict(g) for g in fx]
    for g in fx2:
        if g["provider_game_id"] == "mlb-hou-01":
            g["home_score"] = 4
            g["status_detail"] = "Bot 7th"
        if g["provider_game_id"] == "wc-02":          # scheduled -> live
            g["status"] = core.STATUS_LIVE
            g["status_detail"] = "12'"
        g["broadcast_json"] = resolve_broadcast(g["league"],
                                                (g["tv_national"] or "").split(", "))
    s3 = core.upsert_games(conn, fx2, provider="fixture")
    print("   ", s3)
    check("mutation updates exactly 2", s3["updated"] == 2 and s3["inserted"] == 0)
    total = conn.execute("SELECT COUNT(*) AS n FROM sports_games").fetchone()["n"]
    check("still no duplicate rows after update (6)", total == 6)

    print("Category bucketing (Houston time):")
    today = {g["provider_game_id"] for g in core.get_today(conn)}
    live = {g["provider_game_id"] for g in core.get_live(conn)}
    upcoming = {g["provider_game_id"] for g in core.get_upcoming(conn)}
    completed = {g["provider_game_id"] for g in core.get_completed(conn)}
    previous = {g["provider_game_id"] for g in core.get_previous(conn)}
    print("    today    :", sorted(today))
    print("    live     :", sorted(live))
    print("    upcoming :", sorted(upcoming))
    print("    completed:", sorted(completed))
    print("    previous :", sorted(previous))

    check("World Cup Houston final is in Today", "wc-hou-01" in today)
    check("Astros live game is in Today", "mlb-hou-01" in today)
    check("NBA final from last night is NOT in Today", "nba-fin-01" not in today)
    check("NBA final from last night IS in Previous", "nba-fin-01" in previous)
    check("Astros + NHL + wc-02 (now live) are in Live",
          {"mlb-hou-01", "nhl-01", "wc-02"} <= live)
    check("tomorrow's Astros game is in Upcoming", "mlb-hou-02" in upcoming)
    check("finals appear in Completed", {"wc-hou-01", "nba-fin-01"} <= completed)
    check("today's final NOT in Previous", "wc-hou-01" not in previous)

    print("Favorites (team + league union):")
    core.add_favorite(conn, "manager", "team", "Houston Astros")
    core.add_favorite(conn, "manager", "league", "FIFA World Cup")
    fav = {g["provider_game_id"] for g in core.get_favorites(conn, "manager")}
    print("    favorites:", sorted(fav))
    check("favorites include both Astros games", {"mlb-hou-01", "mlb-hou-02"} <= fav)
    check("favorites include both World Cup games", {"wc-hou-01", "wc-02"} <= fav)
    check("favorites exclude NBA/NHL", not ({"nba-fin-01", "nhl-01"} & fav))
    core.remove_favorite(conn, "manager", "league", "FIFA World Cup")
    fav2 = {g["provider_game_id"] for g in core.get_favorites(conn, "manager")}
    check("removing league favorite drops World Cup games",
          not ({"wc-hou-01", "wc-02"} & fav2) and {"mlb-hou-01"} <= fav2)

    print("Search:")
    res = {g["provider_game_id"] for g in core.search_games(conn, "Astros")}
    check("search 'Astros' finds both games", {"mlb-hou-01", "mlb-hou-02"} <= res)
    res2 = {g["provider_game_id"] for g in core.search_games(conn, "NRG")}
    check("search by venue 'NRG' finds the Houston WC match", "wc-hou-01" in res2)

    print("Serialization shape:")
    sample = core.get_today(conn)[0]
    check("serialized game exposes ct_label", "ct_label" in sample)
    check("serialized game parses broadcast dict", isinstance(sample["broadcast"], dict))
    check("broadcast has directv list", isinstance(sample["broadcast"]["directv"], list))

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        for f in FAIL:
            print("   FAILED:", f)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
