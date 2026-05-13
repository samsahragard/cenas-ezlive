"""Phase 1 / Block 6 — brief composer tests.

Asserts:
  - _fallback_brief renders a valid MorningBrief dict shape (deterministic
    path, used when Claude is unavailable or returns malformed JSON)
  - _validate_brief rejects malformed dicts and accepts well-formed ones
  - gather_signals filters out resolved + acked + cross-store rows and
    sorts severity-DESC
  - compose_brief persists a MorningBrief row even when Anthropic is
    not configured (env var unset → fallback path)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.services import brief_composer as bc
from app.services.brief_composer import (
    AudienceContext,
    SignalForBrief,
    _fallback_brief,
    _validate_brief,
    compose_brief,
    gather_signals,
)
from app.models import Signal, MorningBrief, User


def _audience(role="partner", user_id=1) -> AudienceContext:
    return AudienceContext(
        role=role, user_id=user_id, user_name="Sam",
        store_ids=["store_1", "store_2"],
        store_labels={"store_1": "Copperfield", "store_2": "Tomball"},
        permission_tags={"*"},
        timezone="America/Chicago",
        brief_date=date(2026, 5, 13),
    )


def _signal(severity="warn", **over):
    base = dict(
        rule_key="orders.late_delivery", severity=severity,
        subject_label="Order #X", store_id="store_2",
        store_label="Tomball",
        trigger_at=datetime(2026, 5, 12, 18, 0),
        payload={}, action_text="Call the driver.",
        status="open", acked_by=None, age_hours=12.5,
    )
    base.update(over)
    return SignalForBrief(**base)


# ---- _validate_brief ----

def test_validate_accepts_well_formed_brief():
    d = _fallback_brief(_audience(), signals=[], wins=[])
    assert _validate_brief(d) is True


def test_validate_rejects_non_dict():
    assert _validate_brief("hello") is False
    assert _validate_brief([]) is False


def test_validate_rejects_missing_keys():
    assert _validate_brief({"greeting": "hi"}) is False


def test_validate_rejects_non_list_sections():
    d = _fallback_brief(_audience(), signals=[], wins=[])
    d["sections"] = "string instead of list"
    assert _validate_brief(d) is False


# ---- _fallback_brief shape ----

def test_fallback_brief_quiet_overnight():
    d = _fallback_brief(_audience(), signals=[], wins=[])
    assert "Good morning" in d["greeting"]
    assert "Quiet" in d["headline"] or "0" in d["headline"]
    assert d["sections"] == []
    assert d["fallback_used"] is True


def test_fallback_brief_includes_alerts_section():
    d = _fallback_brief(
        _audience(),
        signals=[_signal(severity="alert")],
        wins=[],
    )
    sections = d["sections"]
    assert sections, "expected at least one section"
    assert sections[0]["section_kind"] == "alerts"
    assert sections[0]["items"][0]["severity"] == "alert"


def test_fallback_brief_includes_wins_section():
    from app.services.brief_composer import WinSignal
    win = WinSignal(
        category="driver", win_key="driver.first_paid_delivery",
        subject_label="Alejandro Martinez", store_id=None, store_label=None,
        occurred_at=datetime.utcnow(), payload={"lifetime_count": 1},
        one_line_seed="Alejandro completed their first paid delivery.",
    )
    d = _fallback_brief(_audience(), signals=[], wins=[win])
    kinds = [s["section_kind"] for s in d["sections"]]
    assert "wins" in kinds
    win_section = next(s for s in d["sections"] if s["section_kind"] == "wins")
    assert win_section["items"][0]["subject_label"] == "Alejandro Martinez"


def test_fallback_brief_caps_wins_at_five(db_session):
    from app.services.brief_composer import WinSignal
    wins = [
        WinSignal(
            category="driver", win_key=f"driver.test_{i}",
            subject_label=f"Driver {i}", store_id=None, store_label=None,
            occurred_at=datetime.utcnow(), payload={},
            one_line_seed=f"Driver {i} did something good.",
        )
        for i in range(8)
    ]
    d = _fallback_brief(_audience(), signals=[], wins=wins)
    win_section = next(s for s in d["sections"] if s["section_kind"] == "wins")
    assert len(win_section["items"]) == 5


# ---- gather_signals filtering ----

def test_gather_signals_excludes_resolved_rows(db_session):
    s = Signal(
        rule_name="orders.late_delivery", severity="warn",
        store_id="store_2", subject_id="ORD-1", subject_label="Order ORD-1",
        trigger_at=datetime(2026, 5, 12, 18, 0),
        payload={}, action_text="-", surfaces=[], audience_roles=[],
        resolved_at=datetime(2026, 5, 12, 19, 0),
    )
    db_session.add(s); db_session.commit()
    out = gather_signals(db_session, _audience())
    assert out == []


def test_gather_signals_excludes_acked_rows(db_session):
    s = Signal(
        rule_name="orders.late_delivery", severity="warn",
        store_id="store_2", subject_id="ORD-2", subject_label="Order ORD-2",
        trigger_at=datetime(2026, 5, 12, 18, 0),
        payload={}, action_text="-", surfaces=[], audience_roles=[],
        acknowledged_by=1,
        acknowledged_at=datetime(2026, 5, 12, 19, 0),
    )
    db_session.add(s); db_session.commit()
    out = gather_signals(db_session, _audience())
    assert out == []


def test_gather_signals_filters_by_store_scope(db_session):
    # Signal scoped to store_3 — audience only has store_1, store_2
    s = Signal(
        rule_name="orders.late_delivery", severity="warn",
        store_id="store_3", subject_id="ORD-3", subject_label="Order ORD-3",
        trigger_at=datetime(2026, 5, 12, 18, 0),
        payload={}, action_text="-", surfaces=[], audience_roles=[],
    )
    db_session.add(s); db_session.commit()
    out = gather_signals(db_session, _audience())
    assert out == []


def test_gather_signals_sorts_severity_desc_then_time(db_session):
    rows = [
        Signal(rule_name="r", severity="info",
               store_id="store_2", subject_id="A", subject_label="A",
               trigger_at=datetime(2026, 5, 12, 10, 0),
               payload={}, action_text="-", surfaces=[], audience_roles=[]),
        Signal(rule_name="r", severity="alert",
               store_id="store_2", subject_id="B", subject_label="B",
               trigger_at=datetime(2026, 5, 12, 9, 0),
               payload={}, action_text="-", surfaces=[], audience_roles=[]),
        Signal(rule_name="r", severity="warn",
               store_id="store_2", subject_id="C", subject_label="C",
               trigger_at=datetime(2026, 5, 12, 23, 0),
               payload={}, action_text="-", surfaces=[], audience_roles=[]),
    ]
    db_session.add_all(rows); db_session.commit()
    out = gather_signals(db_session, _audience())
    severities = [s.severity for s in out]
    assert severities == ["alert", "warn", "info"]


# ---- compose_brief end-to-end ----

def test_compose_brief_persists_fallback_when_anthropic_absent(db_session, monkeypatch):
    # Force the fallback path
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    # Seed a user so _audience.user_id=1 resolves to a real row
    u = User(id=1, full_name="Sam Sahragard", email="sam@x.com",
             passcode_hash="x", permission_level="partner",
             active=True, first_login_done=True)
    db_session.add(u); db_session.commit()

    row = compose_brief(_audience(), db_session)
    db_session.commit()
    assert row.id is not None
    assert row.fallback_used is True
    assert row.composer_model in ("deterministic", bc._MODEL_FALLBACK_TAG)
    persisted = db_session.query(MorningBrief).filter_by(id=row.id).one()
    assert persisted.brief_date == date(2026, 5, 13)
    assert "Good morning" in persisted.body["greeting"]
