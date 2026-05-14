"""Phase 2 / Block 1F — sales-insights synthesis pipeline
(post-Block-1J: a CONSUMER of the AmbientSignal data plane).

samai authored the 1F spec at
/partner/developer/app/block-1f-sales-insights-spec and the 1J spec at
/partner/developer/app/block-1j-ambient-signal-spec; this implements
against both.

1F is the PRODUCER of the ribbon's Sales category: a daily 5am-CT cron
that synthesizes external intelligence into structured SalesInsight
rows. The ribbon's 1C router reads those rows; 1E's every-5m cron
expires them.

Block 1J Day 3 refactored the SOURCE-FETCH stage (1J §5): this module
no longer pulls external sources directly. The six 1J per-source crons
(/cron/refresh-*) write AmbientSignal rows; this pipeline now READS
them. The synthesis + write half is unchanged from 1F.

Pipeline:

  Stage 1 — gather_raw_signals(db): reads the data plane — live
    AmbientSignal rows (valid_until_at >= now), each converted to a
    RawSignal — PLUS the Claude-search adapter, which stays HERE as a
    synthesis INPUT: it produces an interpretive prose digest, not a
    structured data-plane "source signal", so it is not promoted to a
    1J per-source cron (1J §4 / §5 / §12 Q5).

  Stage 2 — _opus_synthesize(): Opus reads the full RawSignal bundle +
    the two store contexts and returns structured insight objects.
    _fallback_insights() is the deterministic degrade path: if Opus is
    unavailable, the fallback-safe structured signals still become
    insight rows — fewer and blunter, not zero. UNCHANGED from 1F (1J
    §5 moves only the source-fetch stage).

  Write — _write_insights(): validate each object against the model's
    allowed values, set valid_until_at per 1F §6, INSERT. Idempotent:
    a same-day re-run replaces, not duplicates. UNCHANGED from 1F.

run_sales_insights_synthesis(db=None) is the entrypoint; the token-
gated POST /cron/sales-insights endpoint (driver_system.py) calls it.

Cost: every Anthropic call's token usage is cost-estimated and summed;
the run summary carries total_cost_usd and flags if it crossed
SALES_INSIGHTS_COST_CEILING_USD (1F §8).
"""
from __future__ import annotations

import json
import logging
import os
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


# ---- AmbientSignal -> RawSignal conversion (Block 1J Day 3) ----
# After 1J, the source-fetch stage is gone — the six 1J per-source
# crons write AmbientSignal rows and this pipeline reads them.
# _ambient_to_raw_signal converts one AmbientSignal into the RawSignal
# shape the Opus synthesis stage already consumes.

# AmbientSignal.category is the 1J ribbon vocabulary
# (caterings|events|maintenance); SalesInsight.category is 1F's
# (weather|events|school_calendar|traffic|outage|yoy_comparison|
# ai_synthesized). The conversion maps by the ambient SOURCE — more
# precise than the ambient category — to a valid 1F category, so the
# _fallback_insights path (which copies structured["category"] straight
# onto the SalesInsight) never produces an invalid row. The
# Opus-synthesis path re-derives the category from content regardless;
# this mapping is the fallback-path safety net. [Flagged for samai —
# the spec leaves the ambient->insight category mapping implicit.]
_AMBIENT_SOURCE_TO_INSIGHT_CATEGORY = {
    "weather": "weather",
    "outages": "outage",
    "traffic": "traffic",
    "events": "events",
    "catering_pipeline": "events",
    "vendor_status": "ai_synthesized",
}


def _ambient_to_raw_signal(signal) -> RawSignal:
    """Convert one AmbientSignal row (written by a 1J /cron/refresh-*
    cron) into the RawSignal shape the synthesis stage consumes. The
    ambient payload's headline/detail become raw_text + structured; an
    AmbientSignal is a concrete structured fact, so the RawSignal is
    marked fallback_safe — it becomes a SalesInsight row even when Opus
    synthesis is unavailable."""
    payload = signal.payload or {}
    headline = (payload.get("headline") or "")
    detail = payload.get("detail") or headline
    insight_category = _AMBIENT_SOURCE_TO_INSIGHT_CATEGORY.get(
        signal.source, "ai_synthesized")
    return RawSignal(
        source=f"ambient:{signal.source}",
        store_scope=signal.store_scope,
        raw_text=(f"{headline}: {detail}" if headline else detail),
        structured={
            "fallback_safe": True,
            "category": insight_category,
            "severity": signal.severity,
            "headline": headline[:200],
            "detail": detail,
            "valid_until": (signal.valid_until_at.isoformat()
                            if signal.valid_until_at else None),
        },
        source_url=payload.get("source_url"),
    )


# ---- Claude-search adapter — STAYS in this pipeline (1J §4/§5/Q5) ----
# Claude-search produces an interpretive prose digest — a synthesis
# INPUT, not a structured data-plane "source signal" like weather or an
# outage — so 1J keeps it here rather than promoting it to a per-source
# /cron/refresh-* cron. It is the only source-fetch left in this module.

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


def gather_raw_signals(db) -> tuple[list[RawSignal], dict, float]:
    """Stage 1 — read the data plane. After Block 1J Day 3 (1J §5),
    sales_insights.py no longer pulls external sources directly: the
    six 1J per-source crons write AmbientSignal rows, and this reads
    the live ones (valid_until_at >= now), each converted to a
    RawSignal via _ambient_to_raw_signal. Claude-search is the one
    source that stays here — a synthesis input, not a data-plane
    source (1J §4 / §5 / §12 Q5).

    Returns (signals, per_source_counts, total_cost_usd). `db` is the
    caller's Session — run_sales_insights_synthesis owns its lifecycle.
    Defensive: an AmbientSignal read failure degrades the run to
    Claude-search only rather than breaking it.
    """
    signals: list[RawSignal] = []
    counts: dict[str, int] = {}
    cost = 0.0
    now = datetime.utcnow()

    # --- the data plane: live AmbientSignal rows the 1J crons wrote ---
    try:
        from app.models import AmbientSignal
        rows = (db.query(AmbientSignal)
                .filter(AmbientSignal.valid_until_at >= now)
                .all())
        for r in rows:
            signals.append(_ambient_to_raw_signal(r))
        counts["ambient_signal"] = len(rows)
    except Exception:  # noqa: BLE001
        logger.warning("sales_insights: AmbientSignal read failed — the "
                       "run degrades to Claude-search only", exc_info=True)
        counts["ambient_signal"] = 0

    # --- Claude-search: a synthesis INPUT, stays in this pipeline ---
    try:
        cs_signals, cs_cost = _fetch_claude_search(_STORE_LOCATIONS)
    except Exception:  # noqa: BLE001
        logger.warning("sales_insights: claude_search adapter raised",
                       exc_info=True)
        cs_signals, cs_cost = [], 0.0
    signals.extend(cs_signals)
    counts["claude_search"] = len(cs_signals)
    cost += cs_cost

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


def _compute_valid_until(now: datetime, hint=None) -> datetime:
    """spec §6 — set valid_until_at. The §6 per-category rules all
    reduce to the same shape: a usable hint wins (the hint is HOW each
    category carries its real end — a weather signal's hint is
    end-of-forecast-day, an event's is the event date, a multi-day
    closure's is its true end); absent a usable hint, the end-of-day CT
    floor applies uniformly. So there is no per-category branching and
    no `category` parameter (samai 1F review obs B). Never returns
    None — valid_until_at is NOT NULL."""
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
            valid_until_at=_compute_valid_until(now, d.get("valid_until")),
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
        # Stage 1 — read the AmbientSignal data plane (1J §5) + the
        # Claude-search synthesis input. The caller's db is passed
        # through so the gather reads live ambient rows.
        raw_signals, adapter_counts, gather_cost = gather_raw_signals(db)

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
