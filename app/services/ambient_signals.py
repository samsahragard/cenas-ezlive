"""Block 1J — the AmbientSignal data plane.

Phase 2 / Block 1J (samai spec, 2026-05-14). The in-app data-plane /
control-plane separation: six per-source /cron/refresh-* crons (Day 2)
WRITE AmbientSignal rows through ambient_signal_upsert(); the 1C
ribbon router + the /cron/sales-insights pipeline READ them. One
producer table, many consumers.

Day 1 (this module) ships the foundation:
  - _ambient_payload_hash() — the canonical change-detector hash (§2.1)
  - ambient_signal_upsert() — the shared id-stable upsert helper (§3)

The id-stable contract (spec §2.2): a re-pull of the same logical
signal — same (source, signal_key) — with a fresh payload UPDATES the
existing row IN PLACE; its id never changes. That stability is what
makes a user's RibbonItemDismissal survive a payload refresh (spec §6,
"the critical invariant").

Pure service module: imports app.models + stdlib only, no Flask.
Import-safe — the Day-2 per-source crons import it freely.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, time, timedelta, timezone

from app.models import (
    AmbientSignal,
    AmbientSignalRun,
    _VALID_AMBIENT_CATEGORIES,
    _VALID_AMBIENT_SEVERITIES,
    _VALID_AMBIENT_SOURCES,
    _VALID_AMBIENT_STORE_SCOPES,
)

logger = logging.getLogger(__name__)


def _ambient_payload_hash(payload: dict) -> str:
    """sha256 of a CANONICAL serialization of ``payload`` — the change
    detector (spec §2.1).

    Canonical (sort_keys + tight separators) so semantically-identical
    payloads hash identically: dict ordering can never cause a spurious
    "changed". All six per-source crons hash through this one function,
    so their hashes are directly comparable.

    The hash covers the payload CONTENT only — row metadata
    (id / last_seen_at / updated_at) lives on the row, not in
    ``payload``, so it is correctly excluded. A non-JSON-serializable
    payload raises (TypeError) — that is a cron bug and the caller's
    per-signal try/except records + skips it (spec §8).
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ambient_signal_upsert(db, *, source: str, signal_key: str,
                          payload: dict, store_scope: str, category: str,
                          severity: str, valid_until_at: datetime) -> str:
    """Upsert one logical ambient signal. Returns ``"created"`` |
    ``"updated"`` | ``"unchanged"`` (spec §2.2). Does NOT commit — the
    calling cron owns the transaction (same split as
    run_escalation_scan / lifecycle.detect_no_shows).

    The three cases, by ``(source, signal_key)`` lookup:
      - no row              -> INSERT a new row,         return "created"
      - row, hash unchanged -> bump last_seen_at only,   return "unchanged"
      - row, hash changed   -> IN-PLACE UPDATE of the    return "updated"
                               SAME row — id NEVER changes

    The "updated" case keeping the same ``id`` is the single most
    important property in 1J: it is what makes the dismissal-survival
    invariant (§6) hold — a RibbonItemDismissal references
    ``(item_type, item_id)``, so a stable id keeps the refreshed signal
    dismissed.

    Validates ``source`` / ``category`` / ``store_scope`` / ``severity``
    against the ``_VALID_AMBIENT_*`` constants; a bad value raises
    ``ValueError`` — the caller's per-signal try/except records it and
    moves on, so one bad signal never aborts the whole cron run (§8).
    """
    if source not in _VALID_AMBIENT_SOURCES:
        raise ValueError(f"bad ambient source {source!r}")
    if category not in _VALID_AMBIENT_CATEGORIES:
        raise ValueError(f"bad ambient category {category!r}")
    if store_scope not in _VALID_AMBIENT_STORE_SCOPES:
        raise ValueError(f"bad ambient store_scope {store_scope!r}")
    if severity not in _VALID_AMBIENT_SEVERITIES:
        raise ValueError(f"bad ambient severity {severity!r}")

    new_hash = _ambient_payload_hash(payload)
    now = datetime.utcnow()

    row = (db.query(AmbientSignal)
           .filter(AmbientSignal.source == source,
                   AmbientSignal.signal_key == signal_key)
           .first())

    # --- created: no row for this logical identity ---
    if row is None:
        db.add(AmbientSignal(
            source=source, signal_key=signal_key, payload=payload,
            payload_hash=new_hash, store_scope=store_scope,
            category=category, severity=severity,
            valid_until_at=valid_until_at,
            created_at=now, updated_at=now, last_seen_at=now,
        ))
        return "created"

    # --- unchanged: same payload hash — NO-OP on content ---
    if row.payload_hash == new_hash:
        # Bump last_seen_at only, so an unchanged-but-still-live signal
        # is not mistaken for stale.
        row.last_seen_at = now
        return "unchanged"

    # --- updated: hash changed — IN-PLACE UPDATE, id never changes ---
    row.payload = payload
    row.payload_hash = new_hash
    row.store_scope = store_scope
    row.category = category
    row.severity = severity
    row.valid_until_at = valid_until_at
    row.updated_at = now
    row.last_seen_at = now
    return "updated"


# ============================================================
# Day 2 — per-source adapters + the shared refresh-cron runner
# ============================================================
# Each /cron/refresh-<source> endpoint (driver_system.py) calls
# run_refresh_cron(db, source, fetch_fn) with one of the six adapters
# below. Adapter contract:
#
#   fetch(db) -> list[dict]
#
# where each dict is the ambient_signal_upsert() kwargs MINUS `source`
# (and `db`): {signal_key, payload, store_scope, category, severity,
# valid_until_at}. Adapters self-guard their external calls — an API /
# scrape failure logs WARN and contributes nothing, and one sub-source
# failing still lets the others through ("degraded, never broken",
# spec §8). run_refresh_cron wraps the whole call as the backstop: an
# adapter that raises PAST its own guard makes the run's
# AmbientSignalRun status="error" (§2.4).
#
# source -> ribbon category: catering_pipeline -> caterings;
# events -> events; weather / outages / traffic / vendor_status ->
# maintenance (operational-condition signals). AmbientSignal.category
# is constrained to caterings|events|maintenance (§2/§7); the spec
# leaves the per-source mapping implicit — FLAGGED for samai's review.

# Move-and-adapted from 1F's sales_insights.py — the two Houston-area
# stores, with coordinates for the geo adapters.
_AMBIENT_STORES = {
    "tomball": {"label": "Tomball", "lat": 30.0972, "lon": -95.6161},
    "copperfield": {"label": "Copperfield", "lat": 29.9165, "lon": -95.6497},
}

# CDT (UTC-5). Fixed offset — does NOT auto-adjust to CST; the same
# limitation as the morning-brief / sales-insights crons. Acceptable
# for an end-of-day expiry floor.
_CT_UTC_OFFSET_HOURS = 5


def _end_of_day_ct(dt: datetime) -> datetime:
    """23:59:59 CT on dt's date, as naive-UTC — the valid_until_at
    floor when nothing else is derivable (spec §2.3)."""
    eod_ct = datetime.combine(dt.date(), time(23, 59, 59))
    return eod_ct + timedelta(hours=_CT_UTC_OFFSET_HOURS)


def _coerce_dt(value, fallback: datetime) -> datetime:
    """An ISO datetime string (possibly tz-aware) or epoch seconds -> a
    naive-UTC datetime; `fallback` on anything unparseable."""
    if isinstance(value, (int, float)):
        try:
            return datetime.utcfromtimestamp(value)
        except (OverflowError, OSError, ValueError):
            return fallback
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return fallback
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return fallback


# ---- minimal Anthropic plumbing — CenterPoint's Haiku-normalize ----
# Move-and-adapted from sales_insights.py: the outages source is a
# JS-heavy public page, so Haiku normalizes the scrape (spec §4).

def _anthropic_client():
    """An anthropic.Anthropic client, or None if the SDK is missing or
    ANTHROPIC_API_KEY is unset."""
    try:
        import anthropic
    except ImportError:
        return None
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _haiku_normalize(raw_text: str, source: str) -> dict:
    """Normalize one unstructured source's messy output into a compact
    structured dict via Haiku. Returns {} on any failure / nothing
    actionable — the caller treats {} as "0 signals" and the run
    degrades cleanly (spec §8)."""
    client = _anthropic_client()
    if client is None or not (raw_text or "").strip():
        return {}
    system = (
        "You normalize messy operational-intelligence source text into "
        "one compact JSON object, or an empty object {} if the text has "
        "no concrete actionable signal. Output ONLY the JSON object — "
        "no prose, no markdown fences. Schema when there IS a signal: "
        '{"store_scope": "tomball"|"copperfield"|"both", '
        '"severity": "info"|"warn"|"alert", '
        '"headline": short ribbon-renderable string, '
        '"detail": one or two sentences, '
        '"signal_key_suffix": a short STABLE identifier for this '
        'specific signal (an area / zip / id — never the changing '
        'customer count)}'
    )
    user = f"SOURCE: {source}\n\nRAW TEXT:\n{(raw_text or '')[:6000]}"
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5", max_tokens=600,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", None) == "text").strip()
    except Exception:  # noqa: BLE001
        logger.warning("ambient: Haiku normalize failed (%s)", source,
                       exc_info=True)
        return {}
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ---- the six per-source adapters ----

def _fetch_weather(db) -> list[dict]:
    """weather AmbientSignals — OpenWeatherMap One Call 3.0 per store
    (the day's forecast + any OWM alerts) + NOAA active severe-weather
    alerts for Texas. Each sub-source self-guards: an OWM failure still
    lets NOAA through (partial degradation, §8). Category: maintenance.
    OWM creds (OPENWEATHERMAP_API_KEY) are in the Render env."""
    out: list[dict] = []
    now = datetime.utcnow()
    eod = _end_of_day_ct(now)

    # --- OpenWeatherMap One Call 3.0 — per-store daily forecast ---
    owm_key = os.getenv("OPENWEATHERMAP_API_KEY", "").strip()
    if owm_key:
        for slug, loc in _AMBIENT_STORES.items():
            try:
                import requests
                r = requests.get(
                    "https://api.openweathermap.org/data/3.0/onecall",
                    params={"lat": loc["lat"], "lon": loc["lon"],
                            "appid": owm_key, "units": "imperial",
                            "exclude": "minutely,hourly"},
                    timeout=15)
                r.raise_for_status()
                data = r.json() or {}
            except Exception:  # noqa: BLE001
                logger.warning("ambient weather: OWM failed for %s", slug,
                               exc_info=True)
                continue
            daily = data.get("daily") or []
            if daily:
                today = daily[0]
                temp = today.get("temp", {}) or {}
                desc = ((today.get("weather") or [{}])[0]
                        .get("description", "weather"))
                high, low = temp.get("max"), temp.get("min")
                hi = round(high) if high is not None else None
                lo = round(low) if low is not None else None
                headline = f"{loc['label']}: {desc}"
                if hi is not None:
                    headline += f", high {hi}F"
                out.append({
                    "signal_key": f"{slug}:forecast:{now.date().isoformat()}",
                    "payload": {
                        "headline": headline[:200],
                        "detail": (f"{loc['label']} forecast: {desc}, high "
                                   f"{hi if hi is not None else '?'}F / low "
                                   f"{lo if lo is not None else '?'}F."),
                        "high_f": hi, "low_f": lo, "conditions": desc,
                    },
                    "store_scope": slug, "category": "maintenance",
                    "severity": "info", "valid_until_at": eod,
                })
            for alert in (data.get("alerts") or []):
                ev = alert.get("event") or "Weather alert"
                out.append({
                    "signal_key": f"{slug}:owm-alert:{ev}".lower()[:200],
                    "payload": {
                        "headline": f"{loc['label']}: {ev}"[:200],
                        "detail": (alert.get("description") or ev)[:600],
                        "event": ev,
                    },
                    "store_scope": slug, "category": "maintenance",
                    "severity": "alert",
                    "valid_until_at": _coerce_dt(alert.get("end"), eod),
                })

    # --- NOAA active severe-weather alerts for Texas ---
    try:
        import requests
        r = requests.get(
            "https://api.weather.gov/alerts/active", params={"area": "TX"},
            headers={"User-Agent": "CenasKitchen/1.0 (ops@cenaskitchen.com)"},
            timeout=15)
        r.raise_for_status()
        features = (r.json() or {}).get("features", []) or []
    except Exception:  # noqa: BLE001
        logger.warning("ambient weather: NOAA failed", exc_info=True)
        features = []
    for feat in features:
        props = (feat or {}).get("properties", {}) or {}
        area = (props.get("areaDesc") or "").lower()
        if not any(k in area for k in
                   ("harris", "montgomery", "houston", "texas")):
            continue
        event = props.get("event") or "Weather alert"
        scope = ("tomball" if ("montgomery" in area and "harris" not in area)
                 else "both")
        sev = (props.get("severity") or "").lower()
        severity = ("alert" if sev in ("extreme", "severe")
                    else "warn" if sev == "moderate" else "info")
        out.append({
            "signal_key": f"noaa:{(feat or {}).get('id') or event}"[:200],
            "payload": {
                "headline": event[:200],
                "detail": (props.get("headline") or event)[:600],
                "event": event,
            },
            "store_scope": scope, "category": "maintenance",
            "severity": severity,
            "valid_until_at": _coerce_dt(
                props.get("expires") or props.get("ends"), eod),
        })
    return out


def _fetch_outages(db) -> list[dict]:
    """outages AmbientSignals — the CenterPoint Energy outage tracker,
    scraped + Haiku-normalized (spec §4). The endpoint is a JS-heavy
    public page (1F flagged it undocumented), so this best-efforts a
    configurable CENTERPOINT_OUTAGE_URL and degrades to 0 signals
    cleanly. Category: maintenance."""
    url = os.getenv(
        "CENTERPOINT_OUTAGE_URL",
        "https://www.centerpointenergy.com/en-us/residential/"
        "outages-and-emergencies/outage-tracker")
    try:
        import requests
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "CenasKitchen/1.0 (ops@cenaskitchen.com)"})
        r.raise_for_status()
        body = r.text or ""
    except Exception:  # noqa: BLE001
        logger.warning("ambient outages: CenterPoint fetch failed",
                       exc_info=True)
        return []
    structured = _haiku_normalize(body, source="centerpoint")
    if not structured or not structured.get("headline"):
        return []
    scope = structured.get("store_scope")
    if scope not in _VALID_AMBIENT_STORE_SCOPES:
        scope = "both"
    severity = structured.get("severity")
    if severity not in _VALID_AMBIENT_SEVERITIES:
        severity = "warn"
    suffix = (structured.get("signal_key_suffix")
              or structured.get("headline", "outage"))[:80]
    now = datetime.utcnow()
    return [{
        "signal_key": f"centerpoint:{suffix}".lower()[:200],
        "payload": {
            "headline": structured.get("headline", "Power outage")[:200],
            "detail": (structured.get("detail")
                       or structured.get("headline", ""))[:600],
        },
        "store_scope": scope, "category": "maintenance",
        "severity": severity,
        # Outages have a short horizon — end-of-day floor; a refresh
        # 15 min later updates it in place if it's still live.
        "valid_until_at": _end_of_day_ct(now),
    }]


def _fetch_catering_pipeline(db) -> list[dict]:
    """catering_pipeline AmbientSignals — upcoming ScheduledEvent rows
    surfaced as ambient catering signals (the "internal" half of spec
    §4's "ezCater + internal"). The ezCater half is a tracked follow-up
    — no ezCater catering-pipeline adapter exists in 1F to move-and-
    adapt, and the ezCater->ambient-signal mapping isn't spec'd in
    detail. Category: caterings."""
    out: list[dict] = []
    now = datetime.utcnow()
    horizon = now + timedelta(days=14)
    try:
        from app.models import ScheduledEvent
        rows = (db.query(ScheduledEvent)
                .filter(ScheduledEvent.status.in_(("scheduled", "confirmed")))
                .filter(ScheduledEvent.scheduled_at >= now)
                .filter(ScheduledEvent.scheduled_at <= horizon)
                .all())
    except Exception:  # noqa: BLE001
        logger.warning("ambient catering_pipeline: ScheduledEvent read failed",
                       exc_info=True)
        return []
    for ev in rows:
        store = getattr(ev, "store", None)
        scope = store if store in _VALID_AMBIENT_STORE_SCOPES else "both"
        sched = getattr(ev, "scheduled_at", None)
        when = sched.strftime("%b %d %I:%M %p") if sched else "soon"
        out.append({
            "signal_key": f"scheduled_event:{ev.id}",
            "payload": {
                "headline": f"Upcoming: {ev.title}"[:200],
                "detail": (f"{ev.title} — {when}"
                           + (f". {ev.notes}" if getattr(ev, "notes", None)
                              else ""))[:600],
                "scheduled_event_id": ev.id,
            },
            "store_scope": scope, "category": "caterings",
            "severity": "info",
            # Relevant until the event ends (or starts, if no end).
            "valid_until_at": (getattr(ev, "scheduled_end_at", None)
                               or sched or _end_of_day_ct(now)),
        })
    return out


def _fetch_events(db) -> list[dict]:
    """events AmbientSignals — Ticketmaster + Google Calendar. CLEAN
    STUB: both need credentials Sam owes (the Ticketmaster key + the
    GCP service-account JSON, spec §4 / §7). Returns [] until those
    land; the endpoint + Render resource + AmbientSignalRun wiring all
    work, so wiring the real adapter later is a contained follow-up."""
    logger.info("ambient events: credential-pending stub — 0 signals")
    return []


def _fetch_traffic(db) -> list[dict]:
    """traffic AmbientSignals — Google Maps (Routes / Distance Matrix).
    CLEAN STUB: needs the GCP key Sam owes (spec §4). Returns [] until
    it lands."""
    logger.info("ambient traffic: credential-pending stub — 0 signals")
    return []


def _fetch_vendor_status(db) -> list[dict]:
    """vendor_status AmbientSignals — CLEAN STUB by design. Sam #1387
    tagged vendor-status a "Phase 3 expansion ingredient" (spec §4 /
    §12 Q5): the endpoint + Render resource + run-audit are built so
    the wiring is proven, but there is no source adapter until the
    Phase-3 vendor-status feed exists."""
    logger.info("ambient vendor_status: Phase-3 stub — 0 signals")
    return []


# (source, adapter) registry — the /cron/refresh-* endpoints look up
# their fetch_fn here.
_ADAPTERS = {
    "weather": _fetch_weather,
    "outages": _fetch_outages,
    "catering_pipeline": _fetch_catering_pipeline,
    "events": _fetch_events,
    "traffic": _fetch_traffic,
    "vendor_status": _fetch_vendor_status,
}


def run_refresh_cron(db, source: str, fetch_fn=None) -> dict:
    """Run one per-source refresh (spec §4): fetch the source -> upsert
    each signal via ambient_signal_upsert() -> sweep THIS source's
    expired rows -> write one AmbientSignalRun -> return the run
    summary dict.

    Cron-independent (§8): self-contained, no cron calls another. Does
    NOT commit — the /cron/refresh-* endpoint owns the transaction
    (same split as run_escalation_scan). One bad signal is recorded and
    skipped, never aborts the run; an adapter that raises past its own
    guard makes status="error" (§2.4), but the run still records its
    AmbientSignalRun + still does the expiry sweep.
    """
    if fetch_fn is None:
        fetch_fn = _ADAPTERS.get(source)
        if fetch_fn is None:
            raise ValueError(f"no ambient adapter for source {source!r}")

    started = datetime.utcnow()
    created = updated = unchanged = 0
    status = "success"
    error_text = None

    try:
        signals = fetch_fn(db)
    except Exception as e:  # noqa: BLE001
        signals = []
        status = "error"
        error_text = f"adapter raised: {e!r}"[:2000]
        logger.warning("ambient refresh %s: adapter raised past its guard",
                       source, exc_info=True)

    for sig in (signals or []):
        try:
            verdict = ambient_signal_upsert(db, source=source, **sig)
        except Exception as e:  # noqa: BLE001
            # One bad signal never aborts the run (§8).
            if status == "success":
                status = "partial"
            logger.warning("ambient refresh %s: bad signal skipped — %r",
                           source, e)
            continue
        if verdict == "created":
            created += 1
        elif verdict == "updated":
            updated += 1
        else:
            unchanged += 1

    # Per-source expiry sweep (§2.3) — DELETE this source's rows past
    # valid_until_at. Runs regardless of fetch outcome.
    expired = (db.query(AmbientSignal)
               .filter(AmbientSignal.source == source,
                       AmbientSignal.valid_until_at < started)
               .delete(synchronize_session=False))
    expired = int(expired or 0)

    finished = datetime.utcnow()
    db.add(AmbientSignalRun(
        source=source, started_at=started, finished_at=finished,
        status=status, signals_created=created, signals_updated=updated,
        signals_unchanged=unchanged, signals_expired=expired,
        error_text=error_text,
    ))

    return {
        "source": source, "status": status,
        "signals_created": created, "signals_updated": updated,
        "signals_unchanged": unchanged, "signals_expired": expired,
        "error_text": error_text,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
    }
