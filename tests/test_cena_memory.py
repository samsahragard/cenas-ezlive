"""Hermetic tests for cena_memory - exemplar/insight round-trips, staleness, and
the verified-only admission rule. Storage is redirected to a tmp dir via env."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("CENA_L3_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CENA_L3_TODAY", raising=False)
    import app.services.cena_memory as m

    importlib.reload(m)
    return m


def test_exemplar_round_trip(mem):
    mem.promote_exemplar(
        "How many catering orders did tomball have in April?",
        ["SELECT SUM(order_count) FROM daily_sales_summary WHERE store_key='tomball'"],
        "Tomball had 120 catering orders in April.",
        verified_by="eval",
    )
    out = mem.recall("orders for tomball during april")
    assert out["exemplars"], "expected the similar exemplar to be recalled"
    top = out["exemplars"][0]
    assert "tomball" in top["question"].lower()
    assert top["sql_plan"] and "daily_sales_summary" in top["sql_plan"][0]


def test_recall_unrelated_returns_empty(mem):
    mem.promote_exemplar("labor hours last week", ["SELECT 1"], "x", "eval")
    out = mem.recall("what is the wifi password")
    assert out["exemplars"] == []


def test_insight_freshness_flag(mem, monkeypatch):
    mem.add_insight(
        "tomball dinner avg check runs ~$4 over copperfield",
        evidence_date="2026-05-01",
        sql="SELECT ...",
        staleness_days=30,
    )
    # within horizon
    monkeypatch.setenv("CENA_L3_TODAY", "2026-05-20")
    fresh = mem.recall("compare avg check tomball vs copperfield dinner")["insights"]
    assert fresh and fresh[0]["needs_reverify"] is False
    # past horizon -> flagged, not dropped
    monkeypatch.setenv("CENA_L3_TODAY", "2026-07-01")
    stale = mem.recall("compare avg check tomball vs copperfield dinner")["insights"]
    assert stale and stale[0]["needs_reverify"] is True


def test_record_unverified_is_not_an_exemplar(mem):
    mem.record(
        "why were sales soft on tuesday",
        "Traffic was down.",
        "low",
        queries=[{"sql": "SELECT 1"}],
        outcome="diagnosis",
        verified=False,
    )
    # logged for audit, but never recalled as guidance
    assert mem.stats()["investigations"] == 1
    assert mem.recall("why were sales soft on tuesday")["exemplars"] == []


def test_record_verified_becomes_exemplar(mem):
    mem.record(
        "net sales for copperfield on 2026-04-15",
        "Copperfield netted $1,436.49 on 2026-04-15.",
        "high",
        queries=[{"sql": "SELECT net_sales FROM daily_sales_summary WHERE business_date='2026-04-15'"}],
        outcome="lookup",
        verified=True,
    )
    out = mem.recall("copperfield net sales 2026-04-15")
    assert out["exemplars"], "verified investigation should be promoted to an exemplar"
    assert "daily_sales_summary" in out["exemplars"][0]["sql_plan"][0]


def test_promote_dedupes_on_normalized_question(mem):
    mem.promote_exemplar("labor hours tomball last week", ["SELECT 1"], "a", "eval")
    mem.promote_exemplar("LABOR hours, tomball last week!", ["SELECT 2"], "b", "eval")
    assert mem.stats()["exemplars"] == 1
    out = mem.recall("labor hours tomball last week")
    assert out["exemplars"][0]["sql_plan"][0] == "SELECT 2"  # freshest kept
