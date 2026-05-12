"""Toast Analytics API client (the /era/v1/* endpoints — separate
subscription + access type from the standard Web/Partner API in
toast_client.py).

Reads credentials from env vars (Render config):
    TOAST_ANALYTICS_CLIENT_ID
    TOAST_ANALYTICS_CLIENT_SECRET

Auth uses Toast's machine-client OAuth flow with userAccessType =
TOAST_MACHINE_CLIENT. Token is cached in-process and refreshed on demand
(when expired or on 401).

Disk cache lives at TOAST_ANALYTICS_CACHE_DIR env (default
/var/data/toast_analytics on Render, ./toast_analytics_cache locally).
Recent-date results (last 2 calendar days) are treated as not-yet-final
and re-fetched after 30 min — matches the stale-cache pattern from the
standard ToastClient.

See /partner/developer/app/toast-analytics-api for the full reference
of endpoints, body shapes, scope, and rate limits.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

API_HOST = "https://ws-api.toasttab.com"
AUTH_URL = f"{API_HOST}/authentication/v1/authentication/login"
ERA_BASE = f"{API_HOST}/era/v1"

TOKEN_REFRESH_LEEWAY_SEC = 300


def _cache_dir() -> Path:
    base = os.getenv("TOAST_ANALYTICS_CACHE_DIR")
    if base:
        p = Path(base)
    elif Path("/var/data").exists():
        p = Path("/var/data/toast_analytics")
    else:
        p = Path.cwd() / "toast_analytics_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


class ToastAnalyticsError(Exception):
    pass


def _ct_today() -> datetime:
    # Restaurant runs Central Time. UTC-5 (CDT). Approximation good enough
    # for cache freshness decisions; we don't pull in tzdata for this.
    return datetime.utcnow() - timedelta(hours=5)


def _ymd(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def _cache_key(method: str, path: str, body: dict | None) -> str:
    """Stable cache filename from (method, path, body). Body is canonicalized."""
    canonical = json.dumps(body or {}, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha1(f"{method} {path} {canonical}".encode("utf-8")).hexdigest()[:16]
    safe_path = path.replace("/", "_").strip("_")
    return f"{safe_path}__{h}.json"


def _is_recent_window(body: dict | None) -> bool:
    """True if the request covers a date within the last 2 CT days (cache
    invalidation: today/yesterday data is mid-flight and should refresh)."""
    if not body:
        return False
    end_str = body.get("endBusinessDate") or body.get("startBusinessDate")
    if not end_str:
        return False
    try:
        end_dt = datetime.strptime(str(end_str), "%Y%m%d")
        return (_ct_today().date() - end_dt.date()).days <= 2
    except ValueError:
        return False


class ToastAnalyticsClient:
    _instance: "ToastAnalyticsClient | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._auth_lock = threading.Lock()

    @classmethod
    def shared(cls) -> "ToastAnalyticsClient":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    # ---- auth ----
    def _refresh_token(self) -> None:
        client_id = os.getenv("TOAST_ANALYTICS_CLIENT_ID")
        client_secret = os.getenv("TOAST_ANALYTICS_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise ToastAnalyticsError(
                "TOAST_ANALYTICS_CLIENT_ID / TOAST_ANALYTICS_CLIENT_SECRET not set in env"
            )
        body = json.dumps({
            "clientId": client_id,
            "clientSecret": client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }).encode("utf-8")
        req = urllib.request.Request(
            AUTH_URL, data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        tok = (data.get("token") or {})
        token = tok.get("accessToken")
        expires_in = tok.get("expiresIn") or 86400
        if not token:
            raise ToastAnalyticsError(f"Toast analytics auth response missing token: {str(data)[:300]}")
        self._token = token
        self._token_exp = time.time() + float(expires_in)
        log.info("toast-analytics: refreshed token; expires at %s",
                 datetime.fromtimestamp(self._token_exp, timezone.utc).isoformat())

    def _get_token(self) -> str:
        with self._auth_lock:
            if not self._token or time.time() > (self._token_exp - TOKEN_REFRESH_LEEWAY_SEC):
                self._refresh_token()
            return self._token  # type: ignore[return-value]

    # ---- HTTP ----
    def _request(self, method: str, path: str, body: dict | None = None) -> dict | list | str:
        token = self._get_token()
        for attempt in (1, 2):
            data = json.dumps(body).encode("utf-8") if body is not None else None
            req = urllib.request.Request(
                f"{ERA_BASE}{path}",
                data=data, method=method,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1:
                    log.warning("toast-analytics: 401 on %s, forcing token refresh", path)
                    with self._auth_lock:
                        self._token = None
                    token = self._get_token()
                    continue
                err_body = e.read().decode("utf-8", errors="replace")[:400]
                raise ToastAnalyticsError(f"Toast Analytics HTTP {e.code} for {method} {path}: {err_body}")
        raise ToastAnalyticsError("unreachable")

    # ---- cached helpers ----
    def _cached_report(self, post_path: str, get_path: str, body: dict,
                       *, refresh: bool = False, fresh_ttl_min: int = 30) -> list | dict:
        """Two-step pattern: POST returns a bare GUID string, then GET that
        GUID returns the data. Disk-cache the final result keyed by
        (POST path, body). Recent-date queries get the stale-after-30-min
        refresh behavior; older queries cache indefinitely."""
        key = _cache_key("POST", post_path, body)
        cache_path = _cache_dir() / key
        if cache_path.exists() and not refresh:
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if _is_recent_window(body):
                    age_min = (time.time() - cache_path.stat().st_mtime) / 60
                    if cached and age_min < fresh_ttl_min:
                        return cached
                else:
                    return cached
            except Exception:
                pass  # fall through to refetch

        guid = self._request("POST", post_path, body)
        if not isinstance(guid, str):
            raise ToastAnalyticsError(
                f"Expected bare GUID string from POST {post_path}; got {type(guid).__name__}: {str(guid)[:200]}"
            )
        result = self._request("GET", f"{get_path}/{guid}")
        cache_path.write_text(json.dumps(result), encoding="utf-8")
        return result  # type: ignore[return-value]

    # ---- public ----
    def restaurants_info(self, *, refresh: bool = False) -> list:
        """List all restaurants accessible to the token. Rarely changes — cached
        for a day."""
        cache_path = _cache_dir() / "restaurants-information.json"
        if cache_path.exists() and not refresh:
            try:
                age_min = (time.time() - cache_path.stat().st_mtime) / 60
                if age_min < 60 * 24:
                    return json.loads(cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        data = self._request("GET", "/restaurants-information")
        if not isinstance(data, list):
            raise ToastAnalyticsError(f"restaurants-information returned non-list: {str(data)[:200]}")
        cache_path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def _pick_metrics_path(self, start_ymd: str, end_ymd: str) -> str:
        """The /metrics/day endpoint enforces start==end and has the most
        generous rate limit (10/min + 60/hr). For any spanning range we
        fall back to /metrics (custom range, 10/hr)."""
        return "/metrics/day" if start_ymd == end_ymd else "/metrics"

    def metrics(self, start_ymd: str, end_ymd: str, restaurant_ids: list[str],
                *, group_by: list[str] | None = None,
                excluded_restaurant_ids: list[str] | None = None,
                refresh: bool = False) -> list:
        """POST /era/v1/metrics[/day] -> GET. Returns the list of metric
        records (per-restaurant per-date for a span, or per-group rows if
        group_by is set)."""
        body = {
            "startBusinessDate": start_ymd,
            "endBusinessDate": end_ymd,
            "restaurantIds": list(restaurant_ids or []),
            "excludedRestaurantIds": list(excluded_restaurant_ids or []),
            "groupBy": list(group_by or []),
        }
        result = self._cached_report(self._pick_metrics_path(start_ymd, end_ymd),
                                     "/metrics", body, refresh=refresh)
        return result if isinstance(result, list) else []

    def _pick_simple_path(self, base: str, start_ymd: str, end_ymd: str) -> str:
        """For /labor and /menu there is NO custom-range endpoint — only
        /day, /week, /month, /year. Pick the smallest bucket that fits."""
        if start_ymd == end_ymd:
            return f"{base}/day"
        try:
            s = datetime.strptime(start_ymd, "%Y%m%d")
            e = datetime.strptime(end_ymd, "%Y%m%d")
            span = (e - s).days + 1
        except ValueError:
            span = 7  # default to week on parse error
        if span <= 7:
            return f"{base}/week"
        if span <= 31:
            return f"{base}/month"
        return f"{base}/year"

    def labor(self, start_ymd: str, end_ymd: str, restaurant_ids: list[str],
              *, group_by: list[str] | None = None,
              refresh: bool = False) -> list:
        """POST /era/v1/labor[/day] -> GET. group_by must be exactly
        ["EMPLOYEE"] or ["JOB"] (Toast rejects both at once)."""
        body = {
            "startBusinessDate": start_ymd,
            "endBusinessDate": end_ymd,
            "restaurantIds": list(restaurant_ids or []),
            "excludedRestaurantIds": [],
            "groupBy": list(group_by or ["JOB"]),
        }
        result = self._cached_report(self._pick_simple_path("/labor", start_ymd, end_ymd),
                                     "/labor", body, refresh=refresh)
        return result if isinstance(result, list) else []

    def menu(self, start_ymd: str, end_ymd: str, restaurant_ids: list[str],
             *, group_by: list[str] | None = None,
             refresh: bool = False) -> list:
        """POST /era/v1/menu[/day] -> GET. Per-restaurant per-date
        sales + waste aggregates."""
        body = {
            "startBusinessDate": start_ymd,
            "endBusinessDate": end_ymd,
            "restaurantIds": list(restaurant_ids or []),
            "excludedRestaurantIds": [],
            "groupBy": list(group_by or []),
        }
        result = self._cached_report(self._pick_simple_path("/menu", start_ymd, end_ymd),
                                     "/menu", body, refresh=refresh)
        return result if isinstance(result, list) else []


# Convenience period helpers — mirror the shape of toast_reports._period_to_dates
# so the new analytics donuts can re-use the same Today / This Week / Last Week pills.

def period_to_ymd_range(period: str) -> tuple[str, str, str]:
    """Returns (start_ymd, end_ymd, label) for a named period in CT.
    period in {today, week, last_week}. Falls back to today on unknown values."""
    today_ct = _ct_today().date()
    if period == "week":
        # Week = Sun..today (matches the dashboard's existing 'This Week' pill)
        start = today_ct - timedelta(days=today_ct.isoweekday() % 7)
        return start.strftime("%Y%m%d"), today_ct.strftime("%Y%m%d"), "This Week"
    if period == "last_week":
        end = today_ct - timedelta(days=(today_ct.isoweekday() % 7) + 1)
        start = end - timedelta(days=6)
        return start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "Last Week"
    # today
    return today_ct.strftime("%Y%m%d"), today_ct.strftime("%Y%m%d"), "Today"
