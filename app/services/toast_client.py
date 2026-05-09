"""Toast POS API client for the cenas-ezlive Flask app.

Reads credentials from env vars (Render config):
    TOAST_CLIENT_ID
    TOAST_CLIENT_SECRET
    TOAST_RESTAURANT_GUID_TOMBALL
    TOAST_RESTAURANT_GUID_COPPERFIELD

Auth uses Toast's machine-client OAuth flow (no user login). Token is
cached in-process and refreshed on demand (when expired or on 401).

Disk cache lives at TOAST_CACHE_DIR env (default: /var/data/toast on Render,
falls back to ./toast_cache locally) so repeated date-range pulls are instant.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

API_HOST = "https://ws-api.toasttab.com"
AUTH_URL = f"{API_HOST}/authentication/v1/authentication/login"

# Toast tokens are valid ~24h. Refresh 5 minutes before expiry to be safe.
TOKEN_REFRESH_LEEWAY_SEC = 300


def _cache_dir() -> Path:
    base = os.getenv("TOAST_CACHE_DIR")
    if base:
        p = Path(base)
    elif Path("/var/data").exists():
        p = Path("/var/data/toast")
    else:
        p = Path.cwd() / "toast_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def restaurant_guids() -> dict[str, str]:
    """Return a {location: restaurant_guid} map populated from env vars.
    Locations are lowercased keys: 'tomball', 'copperfield'."""
    out = {}
    cop = os.getenv("TOAST_RESTAURANT_GUID_COPPERFIELD")
    tom = os.getenv("TOAST_RESTAURANT_GUID_TOMBALL")
    if cop:
        out["copperfield"] = cop
    if tom:
        out["tomball"] = tom
    return out


class ToastError(Exception):
    pass


class ToastClient:
    """Single-process Toast client with in-memory token cache + disk cache."""

    _instance: "ToastClient | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_exp: float = 0.0  # unix seconds
        self._auth_lock = threading.Lock()

    @classmethod
    def shared(cls) -> "ToastClient":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ---- auth ----
    def _refresh_token(self) -> None:
        client_id = os.getenv("TOAST_CLIENT_ID")
        client_secret = os.getenv("TOAST_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise ToastError("TOAST_CLIENT_ID / TOAST_CLIENT_SECRET not set in env")

        body = json.dumps({
            "clientId": client_id,
            "clientSecret": client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }).encode("utf-8")
        req = urllib.request.Request(
            AUTH_URL, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        token = (data.get("token") or {}).get("accessToken")
        if not token:
            raise ToastError(f"Toast auth response missing token: {str(data)[:300]}")
        self._token = token
        # Decode JWT exp (no signature verification — we trust Toast)
        try:
            payload_b64 = token.split(".")[1] + "==="
            import base64
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            self._token_exp = float(payload.get("exp") or 0)
        except Exception:
            self._token_exp = time.time() + 23 * 3600  # assume ~24h
        log.info("toast: refreshed token; expires at %s",
                 datetime.fromtimestamp(self._token_exp, timezone.utc).isoformat())

    def _get_token(self) -> str:
        with self._auth_lock:
            if not self._token or time.time() > (self._token_exp - TOKEN_REFRESH_LEEWAY_SEC):
                self._refresh_token()
            return self._token  # type: ignore[return-value]

    # ---- HTTP ----
    def _http_get(self, url: str, restaurant_guid: str) -> list | dict:
        token = self._get_token()
        for attempt in (1, 2):
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {token}",
                "Toast-Restaurant-External-ID": restaurant_guid,
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    log.warning("toast: 401 on %s, forcing token refresh", url)
                    with self._auth_lock:
                        self._token = None
                    token = self._get_token()
                    continue
                body = e.read().decode("utf-8", errors="replace")
                raise ToastError(f"Toast HTTP {e.code} for {url}: {body[:300]}")
        raise ToastError("unreachable")

    # ---- endpoints (with disk cache) ----
    def fetch_employees(self, location: str, restaurant_guid: str, refresh: bool = False) -> list:
        path = _cache_dir() / f"employees_{location}.json"
        if path.exists() and not refresh:
            return json.loads(path.read_text(encoding="utf-8"))
        log.info("toast: fetching employees for %s", location)
        data = self._http_get(f"{API_HOST}/labor/v1/employees", restaurant_guid)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data  # type: ignore[return-value]

    def fetch_jobs(self, location: str, restaurant_guid: str, refresh: bool = False) -> list:
        path = _cache_dir() / f"jobs_{location}.json"
        if path.exists() and not refresh:
            return json.loads(path.read_text(encoding="utf-8"))
        log.info("toast: fetching jobs for %s", location)
        data = self._http_get(f"{API_HOST}/labor/v1/jobs", restaurant_guid)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data  # type: ignore[return-value]

    def fetch_time_entries(self, location: str, restaurant_guid: str,
                           start: datetime, end: datetime, refresh: bool = False) -> list:
        """Pull time entries for [start, end] inclusive (one-shot range)."""
        key = f"timeentries_{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}_{location}.json"
        path = _cache_dir() / key
        if path.exists() and not refresh:
            return json.loads(path.read_text(encoding="utf-8"))
        # CDT (UTC-5). Avoids tzdata issues. Range is inclusive on both ends.
        start_iso = start.strftime("%Y-%m-%dT00:00:00.000-0500")
        end_iso = (end + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000-0500")
        url = (f"{API_HOST}/labor/v1/timeEntries"
               f"?startDate={urllib.parse.quote(start_iso)}"
               f"&endDate={urllib.parse.quote(end_iso)}")
        log.info("toast: fetching time entries for %s %s..%s",
                 location, start.date(), end.date())
        data = self._http_get(url, restaurant_guid)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data  # type: ignore[return-value]

    def fetch_orders_for_date(self, location: str, restaurant_guid: str,
                              business_date: str, refresh: bool = False) -> list:
        """business_date is YYYYMMDD."""
        path = _cache_dir() / f"orders_{business_date}_{location}.json"
        if path.exists() and not refresh:
            return json.loads(path.read_text(encoding="utf-8"))
        log.info("toast: fetching orders for %s %s", location, business_date)
        all_orders: list = []
        page = 1
        page_size = 100
        while True:
            url = (f"{API_HOST}/orders/v2/ordersBulk"
                   f"?businessDate={business_date}&pageSize={page_size}&page={page}")
            chunk = self._http_get(url, restaurant_guid)
            if not chunk:
                break
            all_orders.extend(chunk)
            if len(chunk) < page_size:
                break
            page += 1
        path.write_text(json.dumps(all_orders), encoding="utf-8")
        return all_orders
