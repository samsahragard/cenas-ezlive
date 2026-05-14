"""Phase 2 / Block 1F — sales-insights synthesis pipeline.

samai authored the spec at
/partner/developer/app/block-1f-sales-insights-spec; this implements
against that doc.

1F is the PRODUCER of the ribbon's Sales category: a daily 5am-CT cron
that pulls external intelligence (weather, events, school calendars,
traffic, outages, local news) and synthesizes it — via a Haiku-
normalize → Opus-synthesize pipeline — into structured SalesInsight
rows. The ribbon's 1C router reads those rows; 1E's every-5m cron
expires them. 1F only writes them.

Pipeline (spec §4), all in this module:

  Stage 1 — gather_raw_signals(): seven source adapters run in
    parallel, each returning list[RawSignal]. One dead adapter (API
    down, no credential yet, raises) returns [] + logs WARN — it never
    breaks the run. Three adapters wire-complete now (NOAA, CenterPoint,
    Claude-search); four are credential-pending stubs (OpenWeatherMap,
    Ticketmaster, Google Calendar, Google Maps) — each paid adapter
    gets wired in its own later commit as Sam provides credentials.

  Stage 2 — _opus_synthesize(): Opus reads the full RawSignal bundle +
    the two store contexts and returns structured insight objects.
    _fallback_insights() is the deterministic degrade path: if Opus is
    unavailable, the fallback-safe structured signals (a NOAA severe-
    weather alert, a confirmed outage) still become insight rows —
    fewer and blunter, not zero. Mirrors brief_composer's _fallback_brief.

  Write — _write_insights(): validate each object against the model's
    allowed values, set valid_until_at per spec §6, INSERT. Idempotent:
    a same-day re-run first deletes the same-day batch, then re-inserts
    (spec §3 — replace, not duplicate).

run_sales_insights_synthesis(db=None) is the entrypoint; the token-
gated POST /cron/sales-insights endpoint (driver_system.py) calls it.

Cost: every Anthropic call's token usage is cost-estimated and summed;
the run summary carries total_cost_usd and flags if it crossed
SALES_INSIGHTS_COST_CEILING_USD (spec §8).
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from app.db import SessionLocal
from app.models import (
    SalesInsight,
    _VALID_INSIGHT_CATEGORIES,
    _VALID_INSIGHT_SEVERITIES,
    _VALID_INSIGHT_STORE_SCOPES,
)

logger = logging.getLogger(__name__)


# ---- model routing (single source of truth for this module) ----
# Mirrors brief_composer's constants — the codebase's actual model
# strings, not the directive's loose "Opus 4.7" naming.
_MODEL_OPUS = "claude-opus-4-5"
_MODEL_HAIKU = "claude-haiku-4-5"

# The Anthropic server-side web-search tool. Pinned here so a version
# bump is a one-line change; the Claude-search adapter degrades to []
# if the SDK rejects it.
_WEB_SEARCH_TOOL = {"type": "web_search_20250305",
                    "name": "web_search", "max_uses": 4}

# Rough list-price estimates, USD per million tokens. Used ONLY for the
# run-summary cost tripwire (spec §8) — not billing-grade. Update if
# Anthropic pricing shifts.
_COST_RATES = {
    _MODEL_OPUS:  {"in": 5.0, "out": 25.0},
    _MODEL_HAIKU: {"in": 1.0, "out": 5.0},
}

# CDT (UTC-5). Does NOT auto-adjust to CST — same fixed-offset
# limitation as the morning-brief cron; acceptable for an end-of-day
# expiry floor.
_CT_UTC_OFFSET_HOURS = 5

# The two stores — context for the model, coordinates for the (stubbed)
# geo adapters. Coordinates are approximate; the paid-adapter commits
# can refine them.
_STORE_LOCATIONS = {
    "tomball": {
        "label": "Tomball",
        "lat": 30.0972, "lon": -95.6161,
        "city": "Tomball, TX",
        "school_district": "Tomball ISD",
    },
    "copperfield": {
        "label": "Copperfield",
        "lat": 29.9165, "lon": -95.6497,
        "city": "Houston, TX (Copperfield area)",
        "school_district": "Cypress-Fairbanks ISD",
    },
}


# ---- the common RawSignal shape (spec §4) ----

@dataclass
class RawSignal:
    """The uniform shape every adapter returns. spec §4:
    {source, store_scope, raw_text, structured, source_url}.

    `structured` carries parsed facts. For a FALLBACK-SAFE signal (one
    that needs no Opus synthesis to be useful — a NOAA alert, a
    confirmed outage) it additionally carries the keys
    _fallback_insights reads: fallback_safe=True plus category /
    severity / headline / detail / valid_until. Unstructured signals
    (Claude-search prose) leave structured sparse and rely on Opus.
    """
    source: str
    store_scope: str           # tomball | copperfield | both
    raw_text: str
    structured: dict = field(default_factory=dict)
    source_url: str | None = None


# ---- Anthropic call plumbing (generalized from brief_composer) ----

def _anthropic_client():
    """An anthropic.Anthropic client, or None if the SDK is missing or
    ANTHROPIC_API_KEY is unset. Each caller makes its own — the client
    is cheap and this keeps the parallel adapters thread-safe."""
    try:
        import anthropic
    except ImportError:
        return None
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    return anthropic.Anthropic(api_key=key)


def _estimate_cost(model: str, resp) -> float:
    """Rough USD estimate from a response's token usage. Best-effort —
    a tripwire number for the run summary, not an invoice."""
    try:
        u = resp.usage
        rates = _COST_RATES.get(model, {"in": 0.0, "out": 0.0})
        in_tok = (getattr(u, "input_tokens", 0) or 0)
        in_tok += (getattr(u, "cache_read_input_tokens", 0) or 0)
        in_tok += (getattr(u, "cache_creation_input_tokens", 0) or 0)
        out_tok = (getattr(u, "output_tokens", 0) or 0)
        return round((in_tok * rates["in"] + out_tok * rates["out"])
                     / 1_000_000, 6)
    except Exception:  # noqa: BLE001
        return 0.0


def _call_model(client, model: str, system: str, user_message: str,
                *, max_tokens: int, tools=None) -> tuple[object | None, float]:
    """One Anthropic call. Returns (response_or_None, cost_usd). Never
    raises — a failure logs and returns (None, 0.0)."""
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
    )
    if tools:
        kwargs["tools"] = tools
    try:
        resp = client.messages.create(**kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("sales_insights: Anthropic call failed (%s)", model)
        return None, 0.0
    return resp, _estimate_cost(model, resp)


def _extract_text(resp) -> str:
    """Concatenate the text blocks of an Anthropic response (ignoring
    tool-use / tool-result blocks)."""
    if resp is None:
        return ""
    try:
        return "".join(
            b.text for b in resp.content
            if getattr(b, "type", None) == "text"
        )
    except Exception:  # noqa: BLE001
        return ""


def _parse_json_list(text: str) -> list:
    """Parse a JSON array out of a model response, tolerating markdown
    fences and a {"insights": [...]} wrapper. Returns [] on any
    failure — the caller falls back."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        data = json.loads(cleaned)
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def _haiku_normalize(raw_text: str, source: str,
                     store_hint: str = "both") -> tuple[dict, float]:
    """Normalize one unstructured source's messy output into a compact
    structured dict via Haiku. Returns (structured_dict, cost). On any
    failure returns ({}, 0.0) — the caller keeps raw_text and lets the
    Opus synthesis stage cope.

    This is the spec §4 "Haiku pulls raw data" interpretation: Haiku
    normalizes UNSTRUCTURED sources (the CenterPoint scrape, etc.);
    structured APIs (NOAA, the paid geo APIs) are plain parses with no
    model in the loop. [Flagged in spec §12 Q2 for Sam.]
    """
    client = _anthropic_client()
    if client is None or not raw_text.strip():
        return {}, 0.0
    system = (
        "You normalize messy operational-intelligence source text into "
        "one compact JSON object. Output ONLY the JSON object, no prose, "
        "no markdown fences. Schema: "
        '{"store_scope": "tomball"|"copperfield"|"both", '
        '"category": one of '
        '["weather","events","school_calendar","traffic","outage",'
        '"yoy_comparison","ai_synthesized"], '
        '"severity": "info"|"warn"|"alert", '
        '"headline": short ribbon-renderable string, '
        '"detail": one or two sentences, '
        '"valid_until": ISO date or datetime if the text implies an end, '
        'else null, '
        '"fallback_safe": true if this is a concrete confirmed fact '
        '(an outage, a closure) that needs no further synthesis, else false}'
    )
    user = (f"SOURCE: {source}\nSTORE HINT: {store_hint}\n\n"
            f"RAW TEXT:\n{raw_text[:6000]}")
    resp, cost = _call_model(client, _MODEL_HAIKU, system, user,
                             max_tokens=600)
    text = _extract_text(resp)
    if not text:
        return {}, cost
    parsed = _parse_json_list("[" + text + "]") if text.strip().startswith("{") else []
    if parsed and isinstance(parsed[0], dict):
        return parsed[0], cost
    # last resort: direct json.loads of a bare object
    try:
        obj = json.loads(text.strip())
        return (obj if isinstance(obj, dict) else {}), cost
    except Exception:  # noqa: BLE001
        return {}, cost


# ---- the seven source adapters (spec §5) ----
# Uniform signature: fetch(store_locations) -> list[RawSignal]
#   or (list[RawSignal], cost_usd) when the adapter spends model tokens.
# Every adapter guards itself: a failure returns [] (or ([], 0.0)) and
# logs WARN. _run_adapter normalizes the two return shapes.

_NOAA_URL = "https://api.weather.gov/alerts/active"
_NOAA_HEADERS = {"User-Agent": "CenasKitchen/1.0 (ops@cenaskitchen.com)"}


def _noaa_store_scope(area_desc: str) -> str:
    """Map a NOAA areaDesc to a store_scope. Both stores sit in the
    Houston / Harris-County area; Tomball also borders Montgomery
    County. Anything else TX-wide → both."""
    a = (area_desc or "").lower()
    if "montgomery" in a and "harris" not in a:
        return "tomball"
    return "both"


def _noaa_severity(noaa_sev: str) -> str:
    s = (noaa_sev or "").lower()
    if s in ("extreme", "severe"):
        return "alert"
    if s == "moderate":
        return "warn"
    return "info"


def _fetch_noaa(store_locations) -> list[RawSignal]:
    """Adapter 5 — NOAA active severe-weather alerts for Texas. Free,
    public JSON feed, wire-complete. A NOAA alert is a concrete fact,
    so its RawSignal is marked fallback_safe — it becomes an insight
    row even when Opus synthesis is unavailable."""
    try:
        import requests
        resp = requests.get(
            _NOAA_URL, params={"area": "TX"},
            headers=_NOAA_HEADERS, timeout=15,
        )
        resp.raise_for_status()
        features = (resp.json() or {}).get("features", []) or []
    except Exception:  # noqa: BLE001
        logger.warning("sales_insights: NOAA adapter failed", exc_info=True)
        return []

    out: list[RawSignal] = []
    for feat in features:
        props = (feat or {}).get("properties", {}) or {}
        event = props.get("event") or "Weather alert"
        area_desc = props.get("areaDesc") or ""
        # Only surface alerts that plausibly touch the Houston metro.
        if not any(k in area_desc.lower()
                   for k in ("harris", "montgomery", "houston", "texas")):
            continue
        headline = props.get("headline") or event
        detail = props.get("description") or headline
        severity = _noaa_severity(props.get("severity"))
        scope = _noaa_store_scope(area_desc)
        out.append(RawSignal(
            source="noaa",
            store_scope=scope,
            raw_text=f"{event}: {headline}",
            structured={
                "fallback_safe": True,
                "category": "weather",
                "severity": severity,
                "headline": event if len(event) <= 200 else event[:200],
                "detail": detail,
                "valid_until": props.get("expires") or props.get("ends"),
            },
            source_url=(feat or {}).get("id"),
        ))
    return out


def _fetch_centerpoint(store_locations) -> tuple[list[RawSignal], float]:
    """Adapter 6 — CenterPoint Energy outage tracker. The public outage
    map has no documented data API; this adapter fetches a configurable
    URL (CENTERPOINT_OUTAGE_URL) and runs the response through Haiku to
    normalize it. Wire-complete in structure; the exact endpoint is a
    known soft spot — set CENTERPOINT_OUTAGE_URL to the live data
    endpoint once confirmed. Until then it best-efforts the public
    tracker page and degrades to [] cleanly. [Flagged for samai's 1F
    review — endpoint needs live verification.]"""
    url = os.getenv(
        "CENTERPOINT_OUTAGE_URL",
        "https://www.centerpointenergy.com/en-us/residential/"
        "outages-and-emergencies/outage-tracker",
    )
    try:
        import requests
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "CenasKitchen/1.0 (ops@cenaskitchen.com)"})
        resp.raise_for_status()
        body = resp.text or ""
    except Exception:  # noqa: BLE001
        logger.warning("sales_insights: CenterPoint adapter fetch failed",
                       exc_info=True)
        return [], 0.0
    if not body.strip():
        return [], 0.0
    structured, cost = _haiku_normalize(
        body, source="centerpoint", store_hint="both")
    if not structured or not structured.get("headline"):
        # Nothing actionable found in the page — common when the page
        # is JS-rendered. Degraded, not broken.
        return [], cost
    scope = structured.get("store_scope")
    if scope not in _VALID_INSIGHT_STORE_SCOPES:
        scope = "both"
    sig = RawSignal(
        source="centerpoint",
        store_scope=scope,
        raw_text=structured.get("detail") or structured.get("headline"),
        structured={
            "fallback_safe": bool(structured.get("fallback_safe")),
            "category": "outage",
            "severity": structured.get("severity", "info"),
            "headline": structured.get("headline"),
            "detail": structured.get("detail"),
            "valid_until": structured.get("valid_until"),
        },
        source_url=url,
    )
    return [sig], cost


def _fetch_claude_search(store_locations) -> tuple[list[RawSignal], float]:
    """Adapter 7 — Claude with the web_search tool. Opus searches for
    today's Houston / Tomball local news, events, and community-board
    chatter that could affect restaurant covers, the dinner rush, or
    driver routes, and returns a prose digest. That digest is an
    UNSTRUCTURED RawSignal — the main Opus synthesis stage turns it
    into structured insight rows; it is NOT fallback_safe."""
    client = _anthropic_client()
    if client is None:
        return [], 0.0
    system = (
        "You are a local-intelligence researcher for two Houston-area "
        "restaurants (Tomball, TX and the Copperfield area of Houston, "
        "TX). Using web search, find what is happening TODAY and this "
        "weekend that could affect restaurant traffic, the dinner rush, "
        "or delivery-driver routes: local events, sports games, "
        "festivals, school calendar notes, major road closures, severe "
        "weather, large outages, notable community news. Be concrete and "
        "local. Return a short prose digest grouped by store, with dates. "
        "If you find nothing notable, say so plainly."
    )
    user = (
        "Search the web for today's relevant local intelligence for "
        "Tomball, TX and the Copperfield area of Houston, TX. "
        f"Today is {date.today().isoformat()}."
    )
    resp, cost = _call_model(client, _MODEL_OPUS, system, user,
                             max_tokens=2000, tools=[_WEB_SEARCH_TOOL])
    digest = _extract_text(resp).strip()
    if not digest:
        return [], cost
    return [RawSignal(
        source="claude_search",
        store_scope="both",
        raw_text=digest,
        structured={},          # unstructured — needs the Opus synthesis stage
        source_url=None,
    )], cost


def _stub_adapter(name: str):
    """Build a credential-pending stub adapter — returns [] cleanly
    until its paid-API credential lands and it is wired in a later
    distinct-risk commit (spec §5 / §13)."""
    def _fetch(store_locations) -> list[RawSignal]:
        logger.info("sales_insights: %s adapter is a credential-pending "
                    "stub — contributing nothing", name)
        return []
    _fetch.__name__ = f"_fetch_{name}"
    return _fetch


_fetch_openweathermap = _stub_adapter("openweathermap")
_fetch_ticketmaster = _stub_adapter("ticketmaster")
_fetch_google_calendar = _stub_adapter("google_calendar")
_fetch_google_maps = _stub_adapter("google_maps")


# (adapter_name, fetch_fn) — the registry the parallel gather iterates.
_ADAPTERS: list[tuple[str, object]] = [
    ("noaa", _fetch_noaa),
    ("centerpoint", _fetch_centerpoint),
    ("claude_search", _fetch_claude_search),
    ("openweathermap", _fetch_openweathermap),
    ("ticketmaster", _fetch_ticketmaster),
    ("google_calendar", _fetch_google_calendar),
    ("google_maps", _fetch_google_maps),
]


def _run_adapter(name, fn, store_locations) -> tuple[list[RawSignal], float]:
    """Invoke one adapter, normalizing its return to (list, cost). An
    adapter may return list[RawSignal] or (list, cost). Self-guarding:
    any exception → ([], 0.0) + WARN."""
    try:
        result = fn(store_locations)
    except Exception:  # noqa: BLE001
        logger.warning("sales_insights: adapter %s raised", name,
                       exc_info=True)
        return [], 0.0
    if isinstance(result, tuple):
        sigs, cost = result
        return list(sigs or []), float(cost or 0.0)
    return list(result or []), 0.0


def gather_raw_signals(store_locations) -> tuple[list[RawSignal], dict, float]:
    """Stage 1 — run all seven adapters in parallel (they are I/O-bound
    HTTP / model calls). One dead adapter never breaks the run. Returns
    (signals, per_adapter_counts, total_cost_usd)."""
    signals: list[RawSignal] = []
    counts: dict[str, int] = {}
    cost = 0.0
    with ThreadPoolExecutor(max_workers=len(_ADAPTERS)) as ex:
        futures = {
            ex.submit(_run_adapter, name, fn, store_locations): name
            for name, fn in _ADAPTERS
        }
        for fut in as_completed(futures):
            name = futures[fut]
            sigs, c = fut.result()   # _run_adapter never raises
            counts[name] = len(sigs)
            cost += c
            signals.extend(sigs)
    return signals, counts, round(cost, 6)


# ---- Stage 2: Opus synthesis + the deterministic fallback ----

_SYNTHESIS_SYSTEM = """\
You are the Cenas Kitchen sales-insight synthesizer. Once a day you
read a bundle of raw external signals (weather, local events, school
calendars, traffic, outages, local news) for two Houston-area
restaurants and turn them into a short list of structured insight
rows for the operations ribbon.

THE TWO STORES:
  - Tomball: Tomball, TX. School district: Tomball ISD.
  - Copperfield: Copperfield area of Houston, TX. School district:
    Cypress-Fairbanks ISD.

WHAT MATTERS: does this signal change today's covers, the dinner rush,
or delivery-driver routes? A 7pm home football game means a later,
bigger rush. 95F and humid means more delivery, fewer walk-ins. A
major road closure slows driver routes. Translate raw signals into
that operational lens.

OUTPUT: a single JSON array, no prose, no markdown fences. Each element:
  {
    "category": one of weather | events | school_calendar | traffic |
                outage | yoy_comparison | ai_synthesized,
    "store_scope": "tomball" | "copperfield" | "both",
    "severity": "info" | "warn" | "alert",
    "headline": short, ribbon-renderable, <= 200 chars,
    "detail": one to three sentences of operational context,
    "source_url": a source link if one is present in the input, else null,
    "valid_until": ISO date or datetime when this stops being relevant
                   (a Friday game -> that Friday; weather -> end of
                   today), or null to default to end of day
  }

RULES:
  - One insight per distinct signal. Do not merge unrelated signals.
  - Skip a raw signal if it has no plausible effect on either store.
  - severity: alert = act today, warn = plan around it, info = good to
    know. Most insights are info or warn.
  - Be specific and local. "Tomball ISD home football game, 7pm Friday
    — expect a later, heavier dinner rush" beats "there is an event".
  - If the bundle is empty or nothing is relevant, return [].
"""


def _build_synthesis_message(raw_signals: list[RawSignal]) -> str:
    payload = [{
        "source": s.source,
        "store_scope": s.store_scope,
        "raw_text": s.raw_text,
        "structured": s.structured,
        "source_url": s.source_url,
    } for s in raw_signals]
    return (
        "RAW SIGNALS (from the parallel source adapters):\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\n"
        f"Today is {date.today().isoformat()}. Synthesize the insight "
        "rows. Return only the JSON array."
    )


def _opus_synthesize(raw_signals: list[RawSignal]) -> tuple[list[dict], float]:
    """Stage 2 — Opus turns the raw bundle into structured insight
    dicts. Returns (insight_dicts, cost). ([], cost) if Opus is
    unavailable or returns nothing parseable — the caller then falls
    back to _fallback_insights."""
    client = _anthropic_client()
    if client is None or not raw_signals:
        return [], 0.0
    resp, cost = _call_model(
        client, _MODEL_OPUS, _SYNTHESIS_SYSTEM,
        _build_synthesis_message(raw_signals), max_tokens=3000)
    rows = _parse_json_list(_extract_text(resp))
    out = [r for r in rows if isinstance(r, dict)]
    if not out:
        logger.warning("sales_insights: Opus synthesis returned nothing "
                       "parseable — falling back")
    return out, cost


def _fallback_insights(raw_signals: list[RawSignal]) -> list[dict]:
    """Deterministic degrade path (spec §4 fallback) — when Opus is
    unavailable, the fallback-safe structured signals (NOAA alerts, a
    confirmed CenterPoint outage) still become insight rows. Fewer and
    blunter than a synthesized run, never zero. Mirrors brief_composer's
    _fallback_brief."""
    out: list[dict] = []
    for sig in raw_signals:
        s = sig.structured or {}
        if not s.get("fallback_safe"):
            continue
        out.append({
            "category": s.get("category", "ai_synthesized"),
            "store_scope": sig.store_scope,
            "severity": s.get("severity", "info"),
            "headline": s.get("headline") or sig.raw_text[:200],
            "detail": s.get("detail") or sig.raw_text,
            "source_url": sig.source_url,
            "valid_until": s.get("valid_until"),
        })
    return out


# ---- valid_until_at rules (spec §6) ----

def _end_of_day_ct(dt: datetime) -> datetime:
    """23:59:59 CT on dt's date, expressed as a naive-UTC datetime —
    the §6 expiry floor so nothing lingers in the ribbon forever."""
    eod_ct = datetime.combine(dt.date(), time(23, 59, 59))
    return eod_ct + timedelta(hours=_CT_UTC_OFFSET_HOURS)


def _coerce_valid_until(hint, now: datetime) -> datetime | None:
    """A model/source `valid_until` hint → a concrete naive-UTC
    datetime, or None if unparseable. A date-only hint resolves to
    end-of-that-day CT; a datetime hint is used directly (tz-aware →
    naive UTC)."""
    if not hint or not isinstance(hint, str):
        return None
    h = hint.strip()
    try:
        dt = datetime.fromisoformat(h)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    # A bare date ("2026-05-14") parses to midnight — treat as
    # end-of-that-day rather than expiring it at 00:00.
    if len(h) <= 10 and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
        return _end_of_day_ct(dt)
    return dt


def _compute_valid_until(category: str, now: datetime,
                         hint=None) -> datetime:
    """spec §6 — set valid_until_at. A usable hint wins (this is how a
    multi-day event or road closure carries its real end). Absent a
    hint, every current category defaults to the end-of-day CT floor.
    Never returns None — valid_until_at is NOT NULL."""
    coerced = _coerce_valid_until(hint, now)
    if coerced is not None:
        return coerced
    return _end_of_day_ct(now)


# ---- the synthesis writer ----

def _write_insights(db, insight_dicts: list[dict],
                    now: datetime) -> tuple[list[SalesInsight], int, dict]:
    """Validate each insight dict against the model's allowed values,
    set valid_until_at per §6, and INSERT. Idempotent (spec §3): the
    same-day batch is deleted first, so a same-day re-run replaces
    rather than duplicates. Returns (written_rows, superseded_count,
    per_category_counts)."""
    # --- idempotency: drop today's existing batch first ---
    day_start = datetime.combine(now.date(), time.min)
    day_end = day_start + timedelta(days=1)
    superseded = (
        db.query(SalesInsight)
        .filter(SalesInsight.created_at >= day_start,
                SalesInsight.created_at < day_end)
        .delete(synchronize_session=False)
    )

    written: list[SalesInsight] = []
    by_category: dict[str, int] = {}
    for d in insight_dicts:
        if not isinstance(d, dict):
            continue
        category = (d.get("category") or "").strip()
        store_scope = (d.get("store_scope") or "").strip()
        severity = (d.get("severity") or "info").strip()
        headline = (d.get("headline") or "").strip()
        if category not in _VALID_INSIGHT_CATEGORIES:
            logger.warning("sales_insights: dropping row, bad category %r",
                           category)
            continue
        if store_scope not in _VALID_INSIGHT_STORE_SCOPES:
            logger.warning("sales_insights: dropping row, bad store_scope %r",
                           store_scope)
            continue
        if severity not in _VALID_INSIGHT_SEVERITIES:
            severity = "info"
        if not headline:
            logger.warning("sales_insights: dropping row, empty headline")
            continue
        row = SalesInsight(
            created_at=now,
            valid_until_at=_compute_valid_until(
                category, now, d.get("valid_until")),
            category=category,
            store_scope=store_scope,
            severity=severity,
            headline=headline[:200],
            detail=(d.get("detail") or None),
            source_url=(d.get("source_url") or None),
            dismissed_by=[],
        )
        db.add(row)
        written.append(row)
        by_category[category] = by_category.get(category, 0) + 1
    return written, int(superseded or 0), by_category


# ---- entrypoint ----

def run_sales_insights_synthesis(db=None) -> dict:
    """The 1F pipeline entrypoint — gather → synthesize (Opus, or the
    deterministic fallback) → write. Returns an inspectable summary
    dict (spec §3): rows per category, per store, raw-signal counts,
    fallback flag, total estimated cost + ceiling status.

    Opens its own Session when db is None (cron path) and commits;
    when handed a Session it mutates without committing (test path) —
    same convention as brief_composer.compose_brief.
    """
    close_db = False
    if db is None:
        db = SessionLocal()
        close_db = True

    now = datetime.utcnow()
    try:
        raw_signals, adapter_counts, gather_cost = gather_raw_signals(
            _STORE_LOCATIONS)

        insight_dicts, synth_cost = _opus_synthesize(raw_signals)
        fallback_used = False
        if not insight_dicts:
            insight_dicts = _fallback_insights(raw_signals)
            fallback_used = True

        written, superseded, by_category = _write_insights(
            db, insight_dicts, now)

        by_store: dict[str, int] = {}
        for r in written:
            by_store[r.store_scope] = by_store.get(r.store_scope, 0) + 1

        if close_db:
            db.commit()

        total_cost = round(gather_cost + synth_cost, 6)
        ceiling = float(os.getenv("SALES_INSIGHTS_COST_CEILING_USD", "5"))
        ceiling_exceeded = total_cost > ceiling
        if ceiling_exceeded:
            logger.warning(
                "sales_insights: run cost $%.4f exceeded ceiling $%.2f",
                total_cost, ceiling)
        logger.info(
            "sales_insights: synthesized %d rows (fallback=%s), "
            "%d raw signals, est cost $%.4f",
            len(written), fallback_used, len(raw_signals), total_cost)

        return {
            "synthesized_at": now.isoformat(),
            "rows_written": len(written),
            "by_category": by_category,
            "by_store": by_store,
            "raw_signals": len(raw_signals),
            "adapters": adapter_counts,
            "fallback_used": fallback_used,
            "superseded": superseded,
            "total_cost_usd": total_cost,
            "cost_ceiling_usd": ceiling,
            "cost_ceiling_exceeded": ceiling_exceeded,
        }
    finally:
        if close_db:
            db.close()
