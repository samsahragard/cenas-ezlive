"""Sling (getsling.com) API client for the cenas-ezlive Flask app.

Reads token from env var SLING_API_KEY (Render config). The token is
the value of the Authorization header captured from a logged-in session.
Sling tokens last ~30 days; if calls start returning 401, regenerate by
logging into Sling in a browser, capturing the Authorization header from
any /v1/* network request, and updating the env var.

Org id (Cenas Kitchen on Sling) is hardcoded since the account isn't
multi-tenant. Override with SLING_ORG_ID if needed.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

API_HOST = "https://api.getsling.com/v1"
DEFAULT_ORG_ID = 451153  # Cenas Kitchen


def _cache_dir() -> Path:
    base = os.getenv("SLING_CACHE_DIR")
    if base:
        p = Path(base)
    elif Path("/var/data").exists():
        p = Path("/var/data/sling")
    else:
        p = Path.cwd() / "sling_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def org_id() -> int:
    return int(os.getenv("SLING_ORG_ID") or DEFAULT_ORG_ID)


class SlingError(Exception):
    pass


class SlingClient:
    _instance: "SlingClient | None" = None
    _lock = threading.Lock()

    @classmethod
    def shared(cls) -> "SlingClient":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _token(self) -> str:
        t = os.getenv("SLING_API_KEY")
        if not t:
            raise SlingError(
                "SLING_API_KEY not set. Get a fresh token by logging into "
                "app.getsling.com, opening DevTools > Network, finding any "
                "request to api.getsling.com, copying the Authorization "
                "header value, and setting it as the env var."
            )
        return t.strip()

    def _http_get(self, path: str) -> list | dict:
        url = f"{API_HOST}{path}"
        # Cloudflare WAF in front of api.getsling.com blocks plain urllib
        # requests as bots. Mimic a real browser to get past it.
        req = urllib.request.Request(url, headers={
            "Authorization": self._token(),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/147.0.0.0 Safari/537.36"),
            "Origin": "https://app.getsling.com",
            "Referer": "https://app.getsling.com/",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code == 401:
                raise SlingError(
                    "Sling 401: token expired or invalid. Refresh SLING_API_KEY "
                    "from a logged-in browser session."
                )
            raise SlingError(f"Sling HTTP {e.code} for {url}: {body[:300]}")

    # ---- endpoints ----
    def fetch_groups(self, refresh: bool = False) -> list:
        """Locations + positions + 'everyone' buckets. Cached on disk."""
        path = _cache_dir() / "groups.json"
        if path.exists() and not refresh:
            return json.loads(path.read_text(encoding="utf-8"))
        log.info("sling: fetching groups")
        data = self._http_get("/groups")
        path.write_text(json.dumps(data), encoding="utf-8")
        return data  # type: ignore[return-value]

    def fetch_users(self, refresh: bool = False) -> list:
        """All users in the org. Cached on disk."""
        path = _cache_dir() / "users.json"
        if path.exists() and not refresh:
            return json.loads(path.read_text(encoding="utf-8"))
        log.info("sling: fetching users")
        data = self._http_get("/users")
        path.write_text(json.dumps(data), encoding="utf-8")
        return data  # type: ignore[return-value]

    def fetch_calendar(self, start: datetime, end: datetime, refresh: bool = False) -> list:
        """Pull all calendar entries (shifts + availability + leave) for [start, end).

        The /calendar/{orgId}/users/{userId} endpoint returns ORG-WIDE data
        when called with any valid user id (the userId in the path is just
        the request author, not a filter)."""
        oid = org_id()
        # Use Sam's user id as caller — any user works
        uid = int(os.getenv("SLING_USER_ID") or 9442223)
        key = f"calendar_{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}.json"
        path = _cache_dir() / key
        if path.exists() and not refresh:
            return json.loads(path.read_text(encoding="utf-8"))

        start_iso = start.strftime("%Y-%m-%dT00:00:00.000Z")
        end_iso = end.strftime("%Y-%m-%dT00:00:00.000Z")
        dates = f"{start_iso}/{end_iso}"
        url_path = f"/calendar/{oid}/users/{uid}?dates={urllib.parse.quote(dates, safe='/:')}"
        log.info("sling: fetching calendar %s..%s", start.date(), end.date())
        data = self._http_get(url_path)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data  # type: ignore[return-value]
