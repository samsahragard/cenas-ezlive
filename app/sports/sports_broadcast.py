"""
sports_broadcast.py — broadcast / streaming / DirecTV resolution.

This is the curated "encoded knowledge" layer. Live game data (from ESPN /
SportRadar) usually tells us *which network* carries a game; this module maps a
network to its DirecTV channel number and the streaming platforms that carry it,
and fills in each league's default streaming package when the feed is silent.

DirecTV numbers below are NATIONAL numbers corroborated across multiple
June-2026 DirecTV lineup sources. Local broadcast affiliates (ABC/CBS/FOX/NBC/
Telemundo) have market-specific numbers, so they are flagged verified=False with
a note to confirm for the Houston market. Anything we are not sure about is
flagged verified=False — the UI shows the number only when verified is True.

Edit this file (not the data feed) when a rights deal or channel number changes.
"""

# --- DirecTV channel numbers — HOUSTON market (verified June 2026) ------------
# key: normalized network name -> {"number": int|None, "verified": bool, "note": str}
# verified=True means double-confirmed against two independent lineup sources;
# verified=False is a best-known number shown WITHOUT a check until re-confirmed.
# Locals are carried by DirecTV at the station's local channel position.
DIRECTV = {
    "ESPN":              {"number": 206, "verified": True,  "note": ""},
    "ESPN2":             {"number": 209, "verified": True,  "note": ""},
    "ESPNU":             {"number": 208, "verified": True,  "note": ""},
    "ESPNEWS":           {"number": 207, "verified": True,  "note": ""},
    "SEC Network":       {"number": 611, "verified": True,  "note": ""},
    "ACC Network":       {"number": 612, "verified": True,  "note": ""},
    "Big Ten Network":   {"number": 610, "verified": True,  "note": ""},
    "FS1":               {"number": 219, "verified": True,  "note": ""},
    "FS2":               {"number": 618, "verified": True,  "note": "Ultimate tier or above"},
    "TNT":               {"number": 245, "verified": True,  "note": ""},
    "TBS":               {"number": 247, "verified": True,  "note": ""},
    "truTV":             {"number": 246, "verified": True,  "note": ""},
    "USA Network":       {"number": 242, "verified": True,  "note": ""},
    "NFL Network":       {"number": 212, "verified": True,  "note": ""},
    "NFL RedZone":       {"number": 211, "verified": True,  "note": ""},
    "MLB Network":       {"number": 213, "verified": True,  "note": ""},
    "NHL Network":       {"number": 215, "verified": True,  "note": ""},
    "NBA TV":            {"number": 216, "verified": True,  "note": ""},
    "Tennis Channel":    {"number": 217, "verified": True,  "note": ""},
    "Golf Channel":      {"number": 218, "verified": True,  "note": ""},
    "CBS Sports Network":{"number": 221, "verified": True,  "note": ""},
    # Houston local broadcast affiliates (OTA channel position).
    "ABC":               {"number": 13,  "verified": False, "note": "KTRK 13 — confirm for your DirecTV area"},
    "CBS":               {"number": 11,  "verified": True,  "note": "KHOU 11"},
    "NBC":               {"number": 2,   "verified": True,  "note": "KPRC 2"},
    "FOX":               {"number": 26,  "verified": False, "note": "KRIV 26 — confirm for your DirecTV area"},
    "Telemundo":         {"number": 47,  "verified": False, "note": "KTMD 47 — confirm for your DirecTV area"},
    "Univision":         {"number": 45,  "verified": False, "note": "KXLN 45 — confirm for your DirecTV area"},
    "Universo":          {"number": 410, "verified": True,  "note": ""},
    "CW":                {"number": 39,  "verified": True,  "note": "KIAH 39"},
    "Space City Home Network": {"number": 674, "verified": True, "note": "Astros/Rockets RSN — verify active carriage"},
}

# --- Xfinity / Comcast channel numbers — HOUSTON market (zip 77002) -----------
# Same verification convention. number=None means no Xfinity number found in
# the Houston lineup (renders "Not available" unless another carrying network
# covers the game on Xfinity).
XFINITY = {
    "ESPN":              {"number": 633, "verified": True,  "note": ""},
    "ESPN2":             {"number": 634, "verified": True,  "note": ""},
    "ESPNU":             {"number": 725, "verified": True,  "note": ""},
    "ESPNEWS":           {"number": 716, "verified": False, "note": ""},
    "FS1":               {"number": None, "verified": False, "note": "Xfinity number not listed — also on FOX One / Fox app"},
    "FS2":               {"number": None, "verified": False, "note": "Xfinity number not listed"},
    "TNT":               {"number": 636, "verified": True,  "note": ""},
    "TBS":               {"number": 651, "verified": True,  "note": ""},
    "truTV":             {"number": 667, "verified": True,  "note": ""},
    "NBA TV":            {"number": 101, "verified": False, "note": ""},
    "NFL Network":       {"number": 113, "verified": False, "note": ""},
    "NFL RedZone":       {"number": 861, "verified": False, "note": ""},
    "MLB Network":       {"number": 682, "verified": False, "note": ""},
    "NHL Network":       {"number": 124, "verified": False, "note": ""},
    "CBS Sports Network":{"number": 706, "verified": True,  "note": ""},
    "Golf Channel":      {"number": 635, "verified": True,  "note": ""},
    "Tennis Channel":    {"number": 705, "verified": False, "note": ""},
    "SEC Network":       {"number": None, "verified": False, "note": "Xfinity number not listed"},
    "ACC Network":       {"number": None, "verified": False, "note": "Xfinity number not listed"},
    "Big Ten Network":   {"number": 123, "verified": False, "note": ""},
    # Houston local broadcast affiliates (Comcast 600-block).
    "ABC":               {"number": 613, "verified": True,  "note": "KTRK"},
    "CBS":               {"number": 611, "verified": True,  "note": "KHOU"},
    "NBC":               {"number": 612, "verified": True,  "note": "KPRC"},
    "FOX":               {"number": 609, "verified": True,  "note": "KRIV"},
    "Telemundo":         {"number": 606, "verified": True,  "note": "KTMD"},
    "Univision":         {"number": 610, "verified": True,  "note": "KXLN"},
    "Universo":          {"number": 239, "verified": False, "note": ""},
    "CW":                {"number": 605, "verified": True,  "note": "KIAH"},
    "Space City Home Network": {"number": 639, "verified": True, "note": "Astros/Rockets RSN — verify active carriage"},
}

# --- Network -> streaming homes ----------------------------------------------
# Where a given linear network can be streamed. "predicted" platforms are added
# by league defaults below, not here.
NETWORK_STREAMING = {
    "ESPN":   ["ESPN App"],
    "ESPN2":  ["ESPN App"],
    "ESPNU":  ["ESPN App"],
    "ABC":    ["ESPN App"],
    "NBC":    ["Peacock"],
    "FOX":    ["FOX One", "Fox Sports app"],
    "FS1":    ["FOX One", "Fox Sports app"],
    "FS2":    ["FOX One", "Fox Sports app"],
    "CBS":    ["Paramount+"],
    "CBS Sports Network": ["Paramount+"],
    "TNT":    ["Max"],
    "TBS":    ["Max"],
    "truTV":  ["Max"],
    "Telemundo": ["Peacock", "Telemundo app"],
    "Universo":  ["Peacock"],
    "NBA TV":    ["NBA App / League Pass"],
    "NFL Network": ["NFL+"],
    "MLB Network": ["MLB.TV"],
    "NHL Network": ["NHL.TV"],
    "Amazon Prime Video": ["Amazon Prime Video"],
    "Prime Video":        ["Amazon Prime Video"],
    "Apple TV+":          ["Apple TV+"],
    "Netflix":            ["Netflix"],
    "Peacock":            ["Peacock"],
    "Tubi":               ["Tubi (free, select)"],
}

# --- League default streaming packages ---------------------------------------
# Always-relevant streaming options per league (out-of-market, league apps,
# recurring streamer deals). These are marked predicted=True in output because
# they depend on the specific matchup/window — verify against the current season.
LEAGUE_DEFAULT_PACKAGES = {
    "NFL":   ["NFL+", "NFL Sunday Ticket (YouTube)"],
    "NBA":   ["NBA App / League Pass"],
    "WNBA":  ["WNBA League Pass"],
    "MLB":   ["MLB.TV"],
    "NHL":   ["ESPN+"],
    "FIFA World Cup": ["FOX One", "Tubi (free, select)", "Peacock (Spanish)"],
    "MLS":   ["MLS Season Pass (Apple TV)"],
}

# --- Network name normalization ----------------------------------------------
_ALIASES = {
    "espn 2": "ESPN2", "espn2": "ESPN2", "espn-u": "ESPNU", "espn u": "ESPNU",
    "abc network": "ABC", "fox network": "FOX", "fox sports 1": "FS1",
    "fox sports 2": "FS2", "nbc sports": "NBC", "prime video": "Amazon Prime Video",
    "amazon prime": "Amazon Prime Video", "amazon": "Amazon Prime Video",
    "apple tv": "Apple TV+", "apple tv plus": "Apple TV+", "paramount": "Paramount+",
    "max": "Max", "hbo max": "Max", "espn+": "ESPN App", "espn plus": "ESPN App",
    "nba tv": "NBA TV", "nfl network": "NFL Network", "mlb network": "MLB Network",
    "nhl network": "NHL Network", "tnt sports": "TNT", "telemundo deportes": "Telemundo",
}


def normalize_network(name):
    """Map a raw network label from a data feed to a canonical key."""
    if not name:
        return None
    n = name.strip()
    low = n.lower()
    if low in _ALIASES:
        return _ALIASES[low]
    # Title-cased exact matches against known keys
    for key in set(list(DIRECTV) + list(NETWORK_STREAMING)):
        if low == key.lower():
            return key
    return n  # unknown network — keep as-is so it still shows on the card


def _provider_primary(provider_map, networks):
    """First carrying network with a Houston channel number on this provider
    (the game's best linear option there), else an unavailable marker. This is
    what drives the "Not available" state when a game is streaming-only or not
    carried by the provider."""
    for key in networks:
        info = provider_map.get(key)
        if info and info["number"] is not None:
            return {
                "available": True, "number": info["number"], "network": key,
                "verified": info["verified"], "note": info["note"],
            }
    return {"available": False, "number": None, "network": None,
            "verified": False, "note": ""}


def _provider_list(provider_map, networks):
    """Every carrying network this provider actually carries (for the drawer)."""
    out = []
    for key in networks:
        info = provider_map.get(key)
        if info and info["number"] is not None:
            out.append({
                "network": key, "number": info["number"],
                "verified": info["verified"], "note": info["note"],
            })
    return out


def resolve_broadcast(league, networks=None, sport=None):
    """
    Build the broadcast payload for a game. Every game resolves BOTH providers
    for the Houston market — a channel number (with verified flag) or
    available=False, which the UI renders as "Not available".

    Returns:
      {
        "tv":           [ "FOX", "FS1" ],
        "directv":      {"available","number","network","verified","note"},
        "xfinity":      {"available","number","network","verified","note"},
        "directv_all":  [ {"network","number","verified","note"} ],
        "xfinity_all":  [ ... ],
        "streaming":    [ {"platform","predicted","note"} ],
        "streaming_only": bool,   # True when neither provider carries it
      }
    """
    networks = [n for n in (networks or []) if n]
    canon = []
    seen = set()
    for raw in networks:
        key = normalize_network(raw)
        if key and key not in seen:
            seen.add(key)
            canon.append(key)

    directv = _provider_primary(DIRECTV, canon)
    xfinity = _provider_primary(XFINITY, canon)

    streaming = []
    stream_seen = set()

    def add_stream(platform, predicted, note=""):
        if platform and platform not in stream_seen:
            stream_seen.add(platform)
            streaming.append({"platform": platform, "predicted": predicted, "note": note})

    # Streaming homes implied by the carrying networks (high confidence)
    for key in canon:
        for plat in NETWORK_STREAMING.get(key, []):
            add_stream(plat, predicted=False)

    # League default package (lower confidence — depends on matchup/window)
    for plat in LEAGUE_DEFAULT_PACKAGES.get(league, []):
        add_stream(plat, predicted=True, note="depends on matchup — verify")

    return {
        "tv": canon,
        "directv": directv,
        "xfinity": xfinity,
        "directv_all": _provider_list(DIRECTV, canon),
        "xfinity_all": _provider_list(XFINITY, canon),
        "streaming": streaming,
        "streaming_only": not directv["available"] and not xfinity["available"],
    }


if __name__ == "__main__":
    # Quick smoke test — one line per game: the Houston DirecTV + Xfinity
    # channel a manager would tune to, or "Not available".
    def _fmt(p):
        if not p["available"]:
            return "Not available"
        return f"{p['number']} ({p['network']})" + ("" if p["verified"] else " [unverified]")

    for lg, nets in [
        ("FIFA World Cup", ["FS1", "Telemundo"]),
        ("FIFA World Cup", ["FOX", "Telemundo"]),
        ("MLB", ["Space City Home Network"]),
        ("MLB", ["FOX"]),
        ("NBA", ["ABC"]),
        ("NHL", ["TNT"]),
        ("WNBA", ["ESPN"]),
        ("MLS", ["Apple TV+"]),
    ]:
        bc = resolve_broadcast(lg, nets)
        print(f"{lg:16} {'/'.join(nets):28} "
              f"DirecTV: {_fmt(bc['directv']):22} Xfinity: {_fmt(bc['xfinity']):22}"
              f"{'  [streaming-only]' if bc['streaming_only'] else ''}")
