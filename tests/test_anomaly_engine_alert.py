"""Phase 1 / Block 4 — Telegram alert path tests for anomaly_engine.

Asserts _maybe_telegram_alert fires Telegram only on:
  - initial creation of an alert-severity Signal
NEVER on:
  - severity != 'alert'
  - update to an existing open row (Signal already exists, no re-ping)
"""
from __future__ import annotations

import pytest

from app.services import anomaly_engine as engine
from app.services.anomaly_engine import SignalDraft


def _draft(severity="alert", subject_id="ORD-1"):
    return SignalDraft(
        rule_name="orders.no_driver_30min_before",
        severity=severity,
        store_id="dos",
        subject_id=subject_id,
        subject_label=f"Order {subject_id}",
        payload={"x": 1},
        action_text="Assign a driver now.",
        surfaces=["orders.by_store"],
        audience_roles=["partner", "gm"],
    )


def _patch_tg(monkeypatch, sink: list):
    """Replace ezcater_webhook._tg_send with a collector that appends
    every call's text into the list. Returns the sink for assertion."""
    import app.web.ezcater_webhook as ew

    def fake(text):
        sink.append(text)

    monkeypatch.setattr(ew, "_tg_send", fake)
    return sink


def test_new_alert_signal_triggers_telegram(db_session, monkeypatch):
    sink = _patch_tg(monkeypatch, [])
    # Drive run_rule indirectly via a fake spec
    fake_spec = engine.RuleSpec(
        name="orders.no_driver_30min_before",
        bucket="every_5m", severity_default="alert",
        surfaces=["x"], audience_roles=["partner"],
        action_text="-",
        fn=lambda db: [_draft(severity="alert", subject_id="ALERT-NEW-1")],
    )
    stats = engine.run_rule(db_session, fake_spec)
    db_session.commit()
    assert stats["alerted"] == 1
    assert len(sink) == 1
    assert "ALERT" in sink[0]
    assert "ALERT-NEW-1" in sink[0]


def test_repeated_alert_fire_does_not_re_trigger(db_session, monkeypatch):
    sink = _patch_tg(monkeypatch, [])
    fake_spec = engine.RuleSpec(
        name="orders.no_driver_30min_before",
        bucket="every_5m", severity_default="alert",
        surfaces=["x"], audience_roles=["partner"],
        action_text="-",
        fn=lambda db: [_draft(severity="alert", subject_id="ALERT-REPEAT")],
    )
    engine.run_rule(db_session, fake_spec)
    engine.run_rule(db_session, fake_spec)
    db_session.commit()
    # Two runs, same condition — only the FIRST fires Telegram
    assert len(sink) == 1


def test_warn_severity_never_triggers_telegram(db_session, monkeypatch):
    sink = _patch_tg(monkeypatch, [])
    fake_spec = engine.RuleSpec(
        name="orders.late_delivery",
        bucket="every_5m", severity_default="warn",
        surfaces=["x"], audience_roles=["partner"],
        action_text="-",
        fn=lambda db: [_draft(severity="warn", subject_id="WARN-1")],
    )
    stats = engine.run_rule(db_session, fake_spec)
    db_session.commit()
    assert stats["alerted"] == 0
    assert sink == []


def test_info_severity_never_triggers_telegram(db_session, monkeypatch):
    sink = _patch_tg(monkeypatch, [])
    fake_spec = engine.RuleSpec(
        name="orders.late_delivery",
        bucket="every_5m", severity_default="info",
        surfaces=["x"], audience_roles=["partner"],
        action_text="-",
        fn=lambda db: [_draft(severity="info", subject_id="INFO-1")],
    )
    engine.run_rule(db_session, fake_spec)
    db_session.commit()
    assert sink == []


def test_telegram_failure_does_not_break_engine(db_session, monkeypatch):
    """If _tg_send raises (e.g. network down, bad token), the engine
    still inserts the Signal and reports stats — alerter is best-effort.
    """
    import app.web.ezcater_webhook as ew
    def boom(text):
        raise RuntimeError("simulated telegram outage")
    monkeypatch.setattr(ew, "_tg_send", boom)

    fake_spec = engine.RuleSpec(
        name="orders.no_driver_30min_before",
        bucket="every_5m", severity_default="alert",
        surfaces=["x"], audience_roles=["partner"],
        action_text="-",
        fn=lambda db: [_draft(severity="alert", subject_id="BOOM-1")],
    )
    stats = engine.run_rule(db_session, fake_spec)
    db_session.commit()
    # Signal still landed; alerted count still increments (we tried).
    assert stats["saved"] == 1
    assert stats["alerted"] == 1
    # And the row is queryable
    from app.models import Signal
    assert db_session.query(Signal).filter_by(subject_id="BOOM-1").count() == 1
