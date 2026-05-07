# distance math / ETA helpers (or external API later)
from __future__ import annotations

import math
import os
import time
from typing import Optional

import requests

_TRANSIENT_API_STATUSES = {"UNKNOWN_ERROR", "OVER_QUERY_LIMIT"}


class GoogleMapsClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GOOGLE_MAPS_API_KEY")
        if not self.api_key:
            raise ValueError("Missing GOOGLE_MAPS_API_KEY")

    def get_drive_minutes(self, origin: str, destination: str) -> int:
        url = "https://maps.googleapis.com/maps/api/distancematrix/json"
        params = {
            "origins": origin,
            "destinations": destination,
            "mode": "driving",
            "units": "imperial",
            "key": self.api_key,
        }

        last_exc: Exception | None = None
        for attempt in range(3):
            if attempt > 0:
                time.sleep(attempt * 2)  # 2s then 4s
            try:
                response = requests.get(url, params=params, timeout=20)
                response.raise_for_status()
            except requests.exceptions.RequestException as e:
                last_exc = e
                continue

            data = response.json()
            api_status = data.get("status")

            if api_status in _TRANSIENT_API_STATUSES:
                last_exc = ValueError(f"Google Maps transient error: {api_status}")
                continue

            if api_status != "OK":
                raise ValueError(f"Google Maps API error: {api_status}: {data.get('error_message', '')}")

            try:
                element = data["rows"][0]["elements"][0]
            except (KeyError, IndexError) as e:
                raise ValueError(f"Unexpected Google Maps response structure: {e}") from e

            if element.get("status") != "OK":
                raise ValueError(f"Route error for '{origin}' -> '{destination}': {element.get('status')}")

            try:
                seconds = element["duration"]["value"]
            except (KeyError, TypeError) as e:
                raise ValueError(f"Missing duration in Google Maps response: {e}") from e

            return max(1, math.ceil(seconds / 60))

        raise last_exc
