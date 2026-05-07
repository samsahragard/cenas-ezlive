"""
Checks which production store (Store #1 or Store #2) is closer to a customer drop-off address.
Uses Google Routes API (computeRouteMatrix) for real driving distance.

Usage: python ezcater_distance.py "<customer address>" [--order-store 1|2|3|4]
Output: JSON { "closer_store": 1 or 2, "store1_miles": N, "store2_miles": N, "exception": bool, ... }

exception=true means drop-off is closer to the OTHER production store than the one normally used.

Migrated from legacy Distance Matrix API on 2026-05-03 after Google began intermittently
denying legacy-API calls for this project.
"""

import sys
import json
import urllib.request
import urllib.error
import os
import time
from pathlib import Path

STORE1_ADDRESS = "15650 FM 529, Houston, TX 77095"
STORE2_ADDRESS = "27727 Tomball Pkwy, Tomball, TX 77375"

SECRETS_FILE = Path(r"C:\Users\sam\.openclaw\.secrets\google_api_key.txt")

ROUTES_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
FIELD_MASK = "originIndex,destinationIndex,distanceMeters,duration,status,condition"


def get_api_key():
    # Order: GOOGLE_MAPS_API_KEY env (Render-style) -> .secrets file (AiCk) ->
    # GOOGLE_API_KEY env (legacy OpenClaw — points at wrong project on AiCk so
    # last resort only).
    val = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if val:
        return val
    if SECRETS_FILE.exists():
        return SECRETS_FILE.read_text(encoding="utf-8").strip()
    return os.environ.get("GOOGLE_API_KEY", "").strip()


def compute_route_matrix(destination, api_key):
    body = {
        "origins": [
            {"waypoint": {"address": STORE1_ADDRESS}},
            {"waypoint": {"address": STORE2_ADDRESS}},
        ],
        "destinations": [
            {"waypoint": {"address": destination}},
        ],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ROUTES_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": FIELD_MASK,
            "User-Agent": "CenasKitchen-CK/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def call_with_retry(destination, api_key):
    last_err = None
    for attempt in range(2):
        try:
            return compute_route_matrix(destination, api_key)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            if 400 <= e.code < 500:
                raise RuntimeError(f"HTTP {e.code}: {body}")
            last_err = RuntimeError(f"HTTP {e.code}: {body}")
        except urllib.error.URLError as e:
            last_err = RuntimeError(f"Network: {e}")
        except Exception as e:
            last_err = RuntimeError(str(e))
        if attempt == 0:
            time.sleep(3)
    raise last_err


def parse_duration_seconds(s):
    if isinstance(s, str) and s.endswith("s"):
        try:
            return float(s[:-1])
        except ValueError:
            return 0.0
    return 0.0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(json.dumps({"error": "No address provided"}))
        sys.exit(1)

    order_store = None
    address_parts = []
    skip_next = False
    for i, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a == "--order-store" and i + 1 < len(args):
            order_store = int(args[i + 1])
            skip_next = True
        else:
            address_parts.append(a)

    destination = " ".join(address_parts)

    api_key = get_api_key()
    if not api_key:
        print(json.dumps({"error": "Missing GOOGLE_API_KEY (env var or .secrets/google_api_key.txt)"}))
        sys.exit(1)

    try:
        data = call_with_retry(destination, api_key)
    except Exception as e:
        print(json.dumps({"error": f"Routes API request failed: {e}"}))
        sys.exit(1)

    if not isinstance(data, list):
        print(json.dumps({"error": f"Routes API returned non-list response: {str(data)[:300]}"}))
        sys.exit(1)

    by_origin = {}
    for item in data:
        if item.get("destinationIndex", 0) != 0:
            continue
        by_origin[item.get("originIndex", -1)] = item

    if 0 not in by_origin or 1 not in by_origin:
        print(json.dumps({"error": "Routes API response missing store rows", "raw": data[:4]}))
        sys.exit(1)

    e1 = by_origin[0]
    e2 = by_origin[1]

    if e1.get("condition") != "ROUTE_EXISTS" or e2.get("condition") != "ROUTE_EXISTS":
        bad = e1 if e1.get("condition") != "ROUTE_EXISTS" else e2
        print(json.dumps({
            "error": f"Routes API condition: {bad.get('condition', 'UNKNOWN')} status={bad.get('status', {})}",
            "destination": destination,
        }))
        sys.exit(1)

    d1 = round(e1.get("distanceMeters", 0) / 1609.344, 1)
    d2 = round(e2.get("distanceMeters", 0) / 1609.344, 1)

    closer_store = 1 if d1 <= d2 else 2

    exception = False
    if order_store is not None:
        normal_store = 1 if order_store in [1, 3] else 2
        exception = (closer_store != normal_store)

    result = {
        "destination": destination,
        "resolved_destination": destination,
        "store1_miles": d1,
        "store2_miles": d2,
        "store1_minutes": round(parse_duration_seconds(e1.get("duration", "0s")) / 60, 1),
        "store2_minutes": round(parse_duration_seconds(e2.get("duration", "0s")) / 60, 1),
        "closer_store": closer_store,
        "exception": exception,
    }
    if order_store is not None:
        result["order_store"] = order_store
        result["normal_store"] = 1 if order_store in [1, 3] else 2

    print(json.dumps(result))
