"""Phase 2 / Block 1F sales-insights tests — post-Block-1J Day 3.

After 1J Day 3, sales_insights.py is a CONSUMER of the AmbientSignal
data plane: gather_raw_signals(db) reads live AmbientSignal rows + the
Claude-search synthesis input. The old source-pull adapters (NOAA,
CenterPoint, the 4 paid stubs, the parallel _ADAPTERS gather) are gone
— removed in the same commit as this rewrite, per 1J §5.

Covers:
  - valid_until_at rules (1F §6) — unchanged.
  - The Claude-search adapter — stays (1J §4/§5/Q5), network-mocked.
  - _ambient_to_raw_signal + the ambient-source -> insight-category map.
  - gather_raw_signals(db): reads live ambient rows + Claude-search;
    degrades cleanly.
  - §5 PARITY PROOF (samai "go (a)"): a seeded AmbientSignal produces,
    through the new gather, a RawSignal equivalent to what the removed
    1F source-pull adapter produced for the same intelligence.
  - _fallback_insights, _write_insights — unchanged.
  - run_sales_insights_synthesis end-to-end (gather + Opus mocked).
  - Cron auth: POST /cron/sales-insights without CRON_TOKEN -> 403.

External calls (Anthropic) are mocked — no network, no key.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

import pytest

from app.models import AmbientSignal, SalesInsight
import app.services.sales_insights as si


# ============================================================
# Fakes — Anthropic client
# ============================================================

class _FakeBlock:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _FakeUsage:
    input_tokens = 200
    output_tokens = 120
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock("text", text)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResp(self._text)


class _FakeClient:
    """An anthropic.Anthropic stand-in whose every call returns the
    same canned text."""
    def __init__(self, text):
        self.messages = _FakeMessages(text)


def _seed_ambient(db, *, source, signal_key, valid_until_at,
                  headline="h", detail="d", store_scope="both",
                  severity="info"):
    now = datetime.utcnow()
    s = AmbientSignal(
        source=source, signal_key=signal_key,
        payload={"headline": headline, "detail": detail},
        payload_hash="x" * 64, store_scope=store_scope,
        category="maintenance", severity=severity,
        valid_until_at=valid_until_at,
        created_at=now, updated_at=now, last_seen_at=now,
    )
    db.add(s)
    return s


# ============================================================
# §6 — valid_until_at rules (unchanged)
# ============================================================

def test_end_of_day_ct_is_naive_utc():
    eod = si._end_of_day_ct(datetime(2026, 5, 14, 10, 0, 0))
    # 23:59:59 CT on 5/14 == 04:59:59 UTC on 5/15 (CDT, UTC-5).
    assert eod == datetime(2026, 5, 15, 4, 59, 59)
    assert eod.tzinfo is None


def test_compute_valid_until_date_hint_is_end_of_that_day():
    now = datetime(2026, 5, 14, 10, 0, 0)
    assert si._compute_valid_until(now, hint="2026-05-20") == \
        datetime(2026, 5, 21, 4, 59, 59)


def test_compute_valid_until_datetime_hint_used_directly():
    now = datetime(2026, 5, 14, 10, 0, 0)
    assert si._compute_valid_until(now, hint="2026-05-16T18:30:00") == \
        datetime(2026, 5, 16, 18, 30, 0)


def test_compute_valid_until_tzaware_hint_converted_to_naive_utc():
    now = datetime(2026, 5, 14, 10, 0, 0)
    vu = si._compute_valid_until(now, hint="2026-05-16T18:30:00-05:00")
    assert vu == datetime(2026, 5, 16, 23, 30, 0)   # -05:00 -> UTC
    assert vu.tzinfo is None


@pytest.mark.parametrize("hint", [None, "", "garbage", "not-a-date", 12345])
def test_compute_valid_until_no_usable_hint_falls_to_end_of_day(hint):
    now = datetime(2026, 5, 14, 10, 0, 0)
    vu = si._compute_valid_until(now, hint=hint)
    assert vu == datetime(2026, 5, 15, 4, 59, 59)    # end of now's day
    assert vu is not None                            # never NULL


# ============================================================
# Claude-search adapter — STAYS in this pipeline (1J §4/§5/Q5)
# ============================================================

def test_claude_search_adapter_returns_digest(monkeypatch):
    monkeypatch.setattr(
        si, "_anthropic_client",
        lambda: _FakeClient("Tomball ISD home football game 7pm Friday."))
    sigs, cost = si._fetch_claude_search(si._STORE_LOCATIONS)
    assert len(sigs) == 1
    assert "Tomball ISD" in sigs[0].raw_text
    assert sigs[0].structured == {}      # unstructured -> needs Opus stage
    assert cost > 0


def test_claude_search_adapter_empty_without_client(monkeypatch):
    monkeypatch.setattr(si, "_anthropic_client", lambda: None)
    sigs, cost = si._fetch_claude_search(si._STORE_LOCATIONS)
    assert sigs == [] and cost == 0.0


# ============================================================
# Block 1J Day 3 — _ambient_to_raw_signal conversion
# ============================================================

def test_ambient_to_raw_signal_conversion():
    now = datetime(2026, 5, 14, 10, 0, 0)
    sig = AmbientSignal(
        source="weather", signal_key="tomball:forecast:2026-05-14",
        payload={"headline": "95F and humid", "detail": "Hot day.",
                 "source_url": "http://noaa/x"},
        payload_hash="h", store_scope="tomball", category="maintenance",
        severity="warn", valid_until_at=datetime(2026, 5, 15, 1, 0, 0),
        created_at=now, updated_at=now, last_seen_at=now)
    rs = si._ambient_to_raw_signal(sig)
    assert rs.source == "ambient:weather"
    assert rs.store_scope == "tomball"
    assert "95F and humid" in rs.raw_text
    assert rs.structured["fallback_safe"] is True
    assert rs.structured["category"] == "weather"      # source-mapped
    assert rs.structured["severity"] == "warn"
    assert rs.structured["headline"] == "95F and humid"
    assert rs.structured["detail"] == "Hot day."
    assert rs.source_url == "http://noaa/x"


@pytest.mark.parametrize("ambient_source,insight_category", [
    ("weather", "weather"),
    ("outages", "outage"),
    ("traffic", "traffic"),
    ("events", "events"),
    ("catering_pipeline", "events"),
    ("vendor_status", "ai_synthesized"),
])
def test_ambient_source_to_insight_category_mapping(ambient_source,
                                                    insight_category):
    # The fallback path copies structured["category"] straight onto the
    # SalesInsight, so it MUST be a valid 1F category for every source.
    now = datetime(2026, 5, 14, 10, 0, 0)
    sig = AmbientSignal(
        source=ambient_source, signal_key="k",
        payload={"headline": "h", "detail": "d"}, payload_hash="x",
        store_scope="both", category="maintenance", severity="info",
        valid_until_at=now, created_at=now, updated_at=now, last_seen_at=now)
    rs = si._ambient_to_raw_signal(sig)
    assert rs.structured["category"] == insight_category
    assert rs.structured["category"] in si._VALID_INSIGHT_CATEGORIES


# ============================================================
# Block 1J Day 3 — gather_raw_signals(db)
# ============================================================

def test_gather_reads_only_live_ambient_signals(db_session, monkeypatch):
    now = datetime.utcnow()
    _seed_ambient(db_session, source="weather", signal_key="live",
                  valid_until_at=now + timedelta(hours=6))
    _seed_ambient(db_session, source="outages", signal_key="expired",
                  valid_until_at=now - timedelta(hours=1))   # not live
    db_session.commit()
    monkeypatch.setattr(si, "_fetch_claude_search", lambda loc: ([], 0.0))

    signals, counts, cost = si.gather_raw_signals(db_session)
    assert counts["ambient_signal"] == 1     # only the live row
    assert len(signals) == 1
    assert signals[0].source == "ambient:weather"


def test_gather_includes_claude_search(db_session, monkeypatch):
    monkeypatch.setattr(
        si, "_fetch_claude_search",
        lambda loc: ([si.RawSignal("claude_search", "both", "digest", {})],
                     0.03))
    signals, counts, cost = si.gather_raw_signals(db_session)
    assert counts["claude_search"] == 1
    assert cost == 0.03
    assert any(s.source == "claude_search" for s in signals)


def test_gather_degrades_when_claude_search_raises(db_session, monkeypatch):
    def _boom(loc):
        raise RuntimeError("search API down")
    monkeypatch.setattr(si, "_fetch_claude_search", _boom)
    # Must not raise — degrades to whatever ambient rows exist (none here).
    signals, counts, cost = si.gather_raw_signals(db_session)
    assert signals == []
    assert counts["claude_search"] == 0


# ============================================================
# §5 PARITY PROOF — the AmbientSignal-reading path faithfully
# replaces the removed source-pull path (samai "go (a)")
# ============================================================

def test_day3_parity_ambient_path_matches_old_source_pull(db_session,
                                                          monkeypatch):
    """§5 parity proof: a 1J cron writing an AmbientSignal for a given
    piece of intelligence produces — through the new AmbientSignal-
    reading gather — a RawSignal equivalent to what the REMOVED 1F
    source-pull adapter produced for that SAME intelligence.

    The synthesis-relevant content (store_scope + structured
    category/severity/headline/detail/fallback_safe) is identical;
    only `source` differs (a provenance label: the old path's "noaa"
    vs the new path's "ambient:weather"). This is the same-commit
    verification §5 requires that the new path faithfully replaces the
    source-pull path removed in this commit.
    """
    now = datetime(2026, 5, 14, 10, 0, 0)

    # What the OLD 1F _fetch_noaa produced for a Heat Advisory — its
    # synthesis-relevant RawSignal content (store_scope + structured
    # subset), per the pre-1J _fetch_noaa: a Harris-County alert ->
    # store_scope "both", structured fallback_safe + weather +
    # mapped-severity + headline + detail.
    OLD_PATH_STORE_SCOPE = "both"
    OLD_PATH_STRUCTURED = {
        "fallback_safe": True,
        "category": "weather",
        "severity": "warn",
        "headline": "Heat Advisory",
        "detail": "Excessive heat through 8 PM CDT.",
    }

    # The 1J weather cron writes that SAME intelligence as an
    # AmbientSignal: source=weather (-> insight category "weather"),
    # the NOAA headline/detail in the payload, the mapped severity.
    db_session.add(AmbientSignal(
        source="weather", signal_key="both:noaa:heat-advisory",
        payload={"headline": "Heat Advisory",
                 "detail": "Excessive heat through 8 PM CDT."},
        payload_hash="h", store_scope="both", category="maintenance",
        severity="warn", valid_until_at=datetime(2026, 5, 15, 1, 0, 0),
        created_at=now, updated_at=now, last_seen_at=now))
    db_session.commit()
    monkeypatch.setattr(si, "_fetch_claude_search", lambda loc: ([], 0.0))

    signals, _counts, _cost = si.gather_raw_signals(db_session)
    converted = [s for s in signals if s.source.startswith("ambient:")]
    assert len(converted) == 1, "the seeded AmbientSignal -> one RawSignal"
    rs = converted[0]

    # PARITY — synthesis-relevant content matches the old source-pull output.
    assert rs.store_scope == OLD_PATH_STORE_SCOPE
    for key, expected in OLD_PATH_STRUCTURED.items():
        assert rs.structured[key] == expected, f"parity mismatch on {key!r}"


# ============================================================
# _fallback_insights (deterministic degrade path) — unchanged
# ============================================================

def test_fallback_insights_emits_only_fallback_safe():
    signals = [
        si.RawSignal("ambient:weather", "both", "Heat", {
            "fallback_safe": True, "category": "weather", "severity": "warn",
            "headline": "Heat Advisory", "detail": "Hot today.",
            "valid_until": None}),
        si.RawSignal("claude_search", "both", "prose digest", {}),  # not safe
    ]
    out = si._fallback_insights(signals)
    assert len(out) == 1
    assert out[0]["category"] == "weather"
    assert out[0]["headline"] == "Heat Advisory"
    assert out[0]["store_scope"] == "both"


# ============================================================
# the synthesis writer — validation + idempotency (unchanged)
# ============================================================

def test_write_insights_validates_and_inserts(db_session):
    now = datetime(2026, 5, 14, 10, 0, 0)
    dicts = [
        {"category": "weather", "store_scope": "both", "severity": "warn",
         "headline": "Hot", "detail": "95F", "valid_until": None},
        {"category": "BOGUS", "store_scope": "both", "severity": "info",
         "headline": "bad category"},                       # dropped
        {"category": "events", "store_scope": "narnia", "severity": "info",
         "headline": "bad store scope"},                    # dropped
        {"category": "events", "store_scope": "tomball", "severity": "info",
         "headline": ""},                                   # dropped (empty)
        {"category": "events", "store_scope": "tomball", "severity": "huh",
         "headline": "bad severity coerced to info"},        # kept
    ]
    written, superseded, by_cat = si._write_insights(db_session, dicts, now)

    assert len(written) == 2
    assert superseded == 0
    assert by_cat == {"weather": 1, "events": 1}
    rows = db_session.query(SalesInsight).all()
    assert {r.category for r in rows} == {"weather", "events"}
    events_row = [r for r in rows if r.category == "events"][0]
    assert events_row.severity == "info"     # "huh" coerced
    assert events_row.dismissed_by == []


def test_write_insights_idempotent_supersedes_same_day(db_session):
    now = datetime(2026, 5, 14, 10, 0, 0)
    si._write_insights(db_session, [{
        "category": "weather", "store_scope": "both",
        "severity": "info", "headline": "first"}], now)
    db_session.flush()
    written, superseded, _ = si._write_insights(db_session, [{
        "category": "weather", "store_scope": "both",
        "severity": "info", "headline": "second"}], now)
    db_session.flush()

    assert superseded == 1
    rows = db_session.query(SalesInsight).all()
    assert len(rows) == 1
    assert rows[0].headline == "second"


# ============================================================
# run_sales_insights_synthesis — end to end (gather + Opus mocked)
# ============================================================

_OPUS_FIXTURE = json.dumps([
    {"category": "events", "store_scope": "tomball", "severity": "warn",
     "headline": "Tomball ISD home game 7pm Fri", "detail": "Later rush.",
     "source_url": None, "valid_until": "2026-05-15"},
    {"category": "weather", "store_scope": "both", "severity": "info",
     "headline": "95F and humid", "detail": "More delivery, fewer walk-ins.",
     "source_url": None, "valid_until": None},
])


def test_run_synthesis_opus_path(db_session, monkeypatch):
    monkeypatch.setattr(si, "gather_raw_signals", lambda db: (
        [si.RawSignal("ambient:weather", "both", "x", {})],
        {"ambient_signal": 1}, 0.0))
    monkeypatch.setattr(si, "_anthropic_client",
                        lambda: _FakeClient(_OPUS_FIXTURE))

    summary = si.run_sales_insights_synthesis(db_session)

    assert summary["rows_written"] == 2
    assert summary["fallback_used"] is False
    assert summary["by_category"] == {"events": 1, "weather": 1}
    assert summary["by_store"] == {"tomball": 1, "both": 1}
    assert summary["raw_signals"] == 1
    assert summary["total_cost_usd"] > 0
    assert db_session.query(SalesInsight).count() == 2


def test_run_synthesis_fallback_path(db_session, monkeypatch):
    # No Anthropic client -> Opus yields nothing -> deterministic fallback.
    safe_sig = si.RawSignal("ambient:weather", "both", "Heat", {
        "fallback_safe": True, "category": "weather", "severity": "alert",
        "headline": "Excessive Heat Warning", "detail": "Dangerous heat.",
        "valid_until": None})
    monkeypatch.setattr(si, "gather_raw_signals",
                        lambda db: ([safe_sig], {"ambient_signal": 1}, 0.0))
    monkeypatch.setattr(si, "_anthropic_client", lambda: None)

    summary = si.run_sales_insights_synthesis(db_session)

    assert summary["fallback_used"] is True
    assert summary["rows_written"] == 1
    rows = db_session.query(SalesInsight).all()
    assert len(rows) == 1
    assert rows[0].category == "weather"
    assert rows[0].severity == "alert"


def test_run_synthesis_summary_shape(db_session, monkeypatch):
    monkeypatch.setattr(si, "gather_raw_signals", lambda db: ([], {}, 0.0))
    monkeypatch.setattr(si, "_anthropic_client", lambda: None)

    summary = si.run_sales_insights_synthesis(db_session)

    assert set(summary) == {
        "synthesized_at", "rows_written", "by_category", "by_store",
        "raw_signals", "adapters", "fallback_used", "superseded",
        "total_cost_usd", "cost_ceiling_usd", "cost_ceiling_exceeded",
    }
    datetime.fromisoformat(summary["synthesized_at"])


def test_run_synthesis_cost_ceiling_flagged(db_session, monkeypatch, caplog):
    monkeypatch.setattr(si, "gather_raw_signals", lambda db: (
        [si.RawSignal("x", "both", "y", {})], {"x": 1}, 9.0))  # pricey gather
    monkeypatch.setattr(si, "_anthropic_client", lambda: None)
    monkeypatch.setenv("SALES_INSIGHTS_COST_CEILING_USD", "5")

    with caplog.at_level(logging.WARNING):
        summary = si.run_sales_insights_synthesis(db_session)

    assert summary["total_cost_usd"] == 9.0
    assert summary["cost_ceiling_usd"] == 5.0
    assert summary["cost_ceiling_exceeded"] is True
    assert any("exceeded ceiling" in r.getMessage() for r in caplog.records)


def test_run_synthesis_idempotent(db_session, monkeypatch):
    monkeypatch.setattr(si, "gather_raw_signals", lambda db: (
        [si.RawSignal("ambient:weather", "both", "x", {})],
        {"ambient_signal": 1}, 0.0))
    monkeypatch.setattr(si, "_anthropic_client",
                        lambda: _FakeClient(_OPUS_FIXTURE))

    first = si.run_sales_insights_synthesis(db_session)
    assert first["rows_written"] == 2
    assert first["superseded"] == 0

    second = si.run_sales_insights_synthesis(db_session)
    assert second["rows_written"] == 2
    assert second["superseded"] == 2          # replaced, not duplicated
    assert db_session.query(SalesInsight).count() == 2


# ============================================================
# POST /cron/sales-insights — the CRON_TOKEN gate
# ============================================================

def test_cron_sales_insights_requires_token(monkeypatch):
    monkeypatch.setenv("CRON_TOKEN", "secret-test-token")
    os.environ.setdefault("ALLOW_DEV_SECRET", "1")
    os.environ.setdefault("SECRET_KEY", "devkey")
    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.post("/cron/sales-insights").status_code == 403
    bad = client.post("/cron/sales-insights",
                       headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 403
