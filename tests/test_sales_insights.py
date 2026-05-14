"""Phase 2 / Block 1F — sales-insights synthesis tests.

Per spec §10:
  - valid_until_at rules: table-driven over the §6 categories.
  - The 3 free adapters (NOAA, CenterPoint, Claude-search): network-
    mocked tests. The 4 paid adapters: return-[] stub tests.
  - Pipeline orchestration: parallel gather, one dead source degrades
    not breaks.
  - Synthesis output -> row mapping: a fixture Opus output maps to
    validated SalesInsight rows.
  - Fallback path: a failing/absent Opus call still inserts the
    fallback-safe structured signals.
  - Idempotency: a same-day re-run replaces, not duplicates.
  - Cost logging: total_cost_usd surfaced; ceiling breach flagged.
  - Cron auth: POST /cron/sales-insights without CRON_TOKEN -> 403.

External calls (Anthropic, requests) are mocked — no network, no key.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import pytest

from app.models import SalesInsight
import app.services.sales_insights as si


# ============================================================
# Fakes — Anthropic client + requests
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


def _fake_requests_get(payload=None, text=None, raise_status=False):
    class _R:
        status_code = 200

        def raise_for_status(self):
            if raise_status:
                raise RuntimeError("boom")

        def json(self):
            return payload or {}

        @property
        def text(self):
            return text or ""

    def _get(*args, **kwargs):
        return _R()
    return _get


# ============================================================
# §6 — valid_until_at rules
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
# §5 — the three free adapters (network-mocked)
# ============================================================

def test_noaa_adapter_parses_and_filters_alerts(monkeypatch):
    payload = {"features": [
        {"id": "https://api.weather.gov/alerts/X",
         "properties": {
             "event": "Heat Advisory",
             "headline": "Heat Advisory until 8 PM CDT",
             "description": "Hot.", "severity": "Moderate",
             "areaDesc": "Harris, TX",
             "expires": "2026-05-14T20:00:00-05:00"}},
        {"id": "https://api.weather.gov/alerts/Y",
         "properties": {
             "event": "Tornado Warning", "headline": "Tornado Warning",
             "description": "Take cover.", "severity": "Extreme",
             "areaDesc": "Montgomery, TX",
             "expires": "2026-05-14T15:00:00-05:00"}},
        {"id": "Z",
         "properties": {
             "event": "Coastal Flood", "headline": "x", "description": "x",
             "severity": "Minor", "areaDesc": "Galveston Island",
             "expires": None}},
    ]}
    monkeypatch.setattr("requests.get", _fake_requests_get(payload=payload))
    sigs = si._fetch_noaa(si._STORE_LOCATIONS)

    assert len(sigs) == 2          # Galveston Island filtered out
    by_event = {s.structured["headline"]: s for s in sigs}
    heat = by_event["Heat Advisory"]
    assert heat.store_scope == "both"               # Harris -> both
    assert heat.structured["severity"] == "warn"    # Moderate
    assert heat.structured["category"] == "weather"
    assert heat.structured["fallback_safe"] is True
    tornado = by_event["Tornado Warning"]
    assert tornado.store_scope == "tomball"         # Montgomery-only
    assert tornado.structured["severity"] == "alert"  # Extreme


def test_noaa_adapter_handles_fetch_failure(monkeypatch):
    monkeypatch.setattr("requests.get",
                        _fake_requests_get(payload={}, raise_status=True))
    assert si._fetch_noaa(si._STORE_LOCATIONS) == []


def test_centerpoint_adapter_normalizes_via_haiku(monkeypatch):
    monkeypatch.setattr("requests.get",
                        _fake_requests_get(text="<html>outage page</html>"))
    monkeypatch.setattr(si, "_haiku_normalize", lambda *a, **k: ({
        "store_scope": "tomball", "category": "outage", "severity": "warn",
        "headline": "Power outage near Tomball",
        "detail": "1,200 customers affected.",
        "valid_until": "2026-05-14T22:00:00", "fallback_safe": True,
    }, 0.002))
    sigs, cost = si._fetch_centerpoint(si._STORE_LOCATIONS)
    assert len(sigs) == 1 and cost == 0.002
    assert sigs[0].store_scope == "tomball"
    assert sigs[0].structured["category"] == "outage"
    assert sigs[0].structured["fallback_safe"] is True


def test_centerpoint_adapter_empty_when_haiku_finds_nothing(monkeypatch):
    monkeypatch.setattr("requests.get",
                        _fake_requests_get(text="<html>js app</html>"))
    monkeypatch.setattr(si, "_haiku_normalize", lambda *a, **k: ({}, 0.001))
    sigs, cost = si._fetch_centerpoint(si._STORE_LOCATIONS)
    assert sigs == [] and cost == 0.001


def test_centerpoint_adapter_handles_fetch_failure(monkeypatch):
    monkeypatch.setattr("requests.get",
                        _fake_requests_get(text="x", raise_status=True))
    sigs, cost = si._fetch_centerpoint(si._STORE_LOCATIONS)
    assert sigs == [] and cost == 0.0


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


def test_paid_adapters_are_stubs():
    for fn in (si._fetch_openweathermap, si._fetch_ticketmaster,
               si._fetch_google_calendar, si._fetch_google_maps):
        assert fn(si._STORE_LOCATIONS) == []


# ============================================================
# §4 — parallel gather orchestration
# ============================================================

def test_gather_raw_signals_one_dead_adapter_degrades(monkeypatch):
    def good(loc):
        return [si.RawSignal("good", "both", "ok", {})]

    def dead(loc):
        raise RuntimeError("boom")

    def costly(loc):
        return [si.RawSignal("costly", "tomball", "x", {})], 0.05

    monkeypatch.setattr(si, "_ADAPTERS",
                        [("good", good), ("dead", dead), ("costly", costly)])
    signals, counts, cost = si.gather_raw_signals(si._STORE_LOCATIONS)

    assert len(signals) == 2          # good + costly; dead degraded to []
    assert counts == {"good": 1, "dead": 0, "costly": 1}
    assert cost == 0.05


# ============================================================
# §4 — _fallback_insights (deterministic degrade path)
# ============================================================

def test_fallback_insights_emits_only_fallback_safe():
    signals = [
        si.RawSignal("noaa", "both", "Heat", {
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
# the synthesis writer — validation + idempotency
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
    monkeypatch.setattr(si, "gather_raw_signals", lambda loc: (
        [si.RawSignal("noaa", "both", "x", {})], {"noaa": 1}, 0.0))
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
    safe_sig = si.RawSignal("noaa", "both", "Heat", {
        "fallback_safe": True, "category": "weather", "severity": "alert",
        "headline": "Excessive Heat Warning", "detail": "Dangerous heat.",
        "valid_until": None})
    monkeypatch.setattr(si, "gather_raw_signals",
                        lambda loc: ([safe_sig], {"noaa": 1}, 0.0))
    monkeypatch.setattr(si, "_anthropic_client", lambda: None)

    summary = si.run_sales_insights_synthesis(db_session)

    assert summary["fallback_used"] is True
    assert summary["rows_written"] == 1
    rows = db_session.query(SalesInsight).all()
    assert len(rows) == 1
    assert rows[0].category == "weather"
    assert rows[0].severity == "alert"


def test_run_synthesis_summary_shape(db_session, monkeypatch):
    monkeypatch.setattr(si, "gather_raw_signals", lambda loc: ([], {}, 0.0))
    monkeypatch.setattr(si, "_anthropic_client", lambda: None)

    summary = si.run_sales_insights_synthesis(db_session)

    assert set(summary) == {
        "synthesized_at", "rows_written", "by_category", "by_store",
        "raw_signals", "adapters", "fallback_used", "superseded",
        "total_cost_usd", "cost_ceiling_usd", "cost_ceiling_exceeded",
    }
    datetime.fromisoformat(summary["synthesized_at"])


def test_run_synthesis_cost_ceiling_flagged(db_session, monkeypatch, caplog):
    monkeypatch.setattr(si, "gather_raw_signals", lambda loc: (
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
    monkeypatch.setattr(si, "gather_raw_signals", lambda loc: (
        [si.RawSignal("noaa", "both", "x", {})], {"noaa": 1}, 0.0))
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
