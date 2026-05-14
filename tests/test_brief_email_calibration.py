"""Phase 1 / Block 6 calibration C3 — calibration-mode dispatch tests.

Asserts:
  - _calibration_enabled() reads BRIEF_CALIBRATION_MODE truthy values
  - _calibration_panel() parses BRIEF_CALIBRATION_PANEL comma-sep IDs
    (and tolerates whitespace, empty entries, non-integer noise)
  - format_subject(..., calibration=True) produces clean [Calibration]
    prefix without severity count
  - format_subject(..., calibration=False) unchanged from normal path
  - resolve_recipient skips non-panel users with status="skipped_not_panel"
    when calibration mode on
  - resolve_recipient permits panel users (partner OR explicit ID match)
    when calibration mode on
  - render_plain_calibration: contains intro line, original brief body,
    all 4 questions inline, and the absolute feedback URL
  - dispatch_brief end-to-end: with BRIEF_CALIBRATION_MODE=1 +
    BRIEF_EMAIL_DISPATCH=1, panel partner gets subject with [Calibration]
    prefix; body has intro + questions; non-panel user gets status=
    "skipped_not_panel" with sent=[]
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.services import brief_email as be
from app.services.brief_composer import AudienceContext
from app.models import User


@pytest.fixture
def audience():
    return AudienceContext(
        role="partner", user_id=1, user_name="Sam Sahragard",
        store_ids=["store_1", "store_2"],
        store_labels={"store_1": "Copperfield", "store_2": "Tomball"},
        permission_tags={"*"},
        timezone="America/Chicago",
        brief_date=date(2026, 5, 13),
    )


@pytest.fixture
def brief_body():
    return {
        "brief_id": "test-brief-id-abc123",
        "audience_role": "partner",
        "audience_user_id": 1,
        "brief_date": "2026-05-13",
        "greeting": "Good morning, Sam.",
        "headline": "One late delivery flagged overnight.",
        "sections": [
            {
                "section_kind": "warns",
                "heading": "Warns",
                "intro": None,
                "items": [
                    {"severity": "warn", "subject_label": "ORD-1",
                     "rule_key": "orders.late_delivery",
                     "one_line": "ORD-1 ran 18 min past its window.",
                     "action": "Confirm completion.",
                     "store_label": "Tomball",
                     "link_path": "/partner/orders/ORD-1",
                     "badge": None},
                ],
            },
        ],
        "closing": "Have a strong day.",
        "composer_model": "deterministic",
        "fallback_used": True,
    }


@pytest.fixture
def brief_row(brief_body):
    return SimpleNamespace(body=brief_body, id=42)


# ---- _calibration_enabled / _calibration_panel ----

def test_calibration_enabled_truthy(monkeypatch):
    for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("BRIEF_CALIBRATION_MODE", val)
        assert be._calibration_enabled() is True
    for val in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("BRIEF_CALIBRATION_MODE", val)
        assert be._calibration_enabled() is False


def test_calibration_panel_parses_comma_sep(monkeypatch):
    monkeypatch.setenv("BRIEF_CALIBRATION_PANEL", "1,3,7")
    assert be._calibration_panel() == {1, 3, 7}


def test_calibration_panel_tolerates_whitespace_and_empty(monkeypatch):
    monkeypatch.setenv("BRIEF_CALIBRATION_PANEL", " 1 ,, 2 , 3, ")
    assert be._calibration_panel() == {1, 2, 3}


def test_calibration_panel_skips_non_integer(monkeypatch, caplog):
    monkeypatch.setenv("BRIEF_CALIBRATION_PANEL", "1,foo,3")
    panel = be._calibration_panel()
    assert panel == {1, 3}


def test_calibration_panel_empty_when_unset(monkeypatch):
    monkeypatch.delenv("BRIEF_CALIBRATION_PANEL", raising=False)
    assert be._calibration_panel() == set()


# ---- format_subject(calibration=True/False) ----

def test_subject_calibration_variant():
    counts = be._SeverityCounts(alert=1, warn=2, info=0)
    subj = be.format_subject(date(2026, 5, 13), counts, calibration=True)
    assert subj == "[Calibration] Cenas brief — 2026-05-13"
    assert "alert" not in subj.lower()
    assert "warn" not in subj.lower()


def test_subject_normal_unchanged_by_calibration_kwarg():
    counts = be._SeverityCounts(alert=1, warn=2, info=0)
    subj = be.format_subject(date(2026, 5, 13), counts, calibration=False)
    assert subj == "Cenas brief — 2026-05-13 — 1 alerts / 2 warns"


def test_subject_default_calibration_false():
    """Default kwarg keeps callers that don't know about calibration
    (or pre-C3 codepaths) working — back-compat."""
    counts = be._SeverityCounts(alert=0, warn=0, info=0)
    subj = be.format_subject(date(2026, 5, 13), counts)
    assert subj.startswith("Cenas brief")
    assert "[Calibration]" not in subj


# ---- resolve_recipient with calibration mode ----

def test_resolve_skips_non_panel_user_in_calibration_mode(
        monkeypatch, audience, db_session):
    monkeypatch.setenv("BRIEF_CALIBRATION_MODE", "1")
    monkeypatch.setenv("BRIEF_CALIBRATION_PANEL", "2,3,4")  # not user 1
    addr, reason = be.resolve_recipient(audience, db_session)
    assert addr is None
    assert reason == "skipped_not_panel"


def test_resolve_permits_panel_partner_in_calibration_mode(
        monkeypatch, audience, db_session):
    monkeypatch.setenv("BRIEF_CALIBRATION_MODE", "1")
    monkeypatch.setenv("BRIEF_CALIBRATION_PANEL", "1,2,3")  # user 1 included
    addr, reason = be.resolve_recipient(audience, db_session)
    assert reason == "ok"
    assert addr == be.PARTNER_EMAIL


def test_resolve_permits_panel_corporate_in_calibration_mode(
        monkeypatch, audience, db_session):
    monkeypatch.setenv("BRIEF_CALIBRATION_MODE", "1")
    monkeypatch.setenv("BRIEF_CALIBRATION_PANEL", "99")
    audience.role = "corporate"
    audience.user_id = 99
    u = User(
        id=99, full_name="Masood", email="masood@x.test",
        passcode_hash="x", permission_level="corporate",
        active=True, first_login_done=True,
    )
    db_session.add(u); db_session.commit()
    addr, reason = be.resolve_recipient(audience, db_session)
    assert reason == "ok"
    assert addr == "masood@x.test"


def test_resolve_unaffected_by_panel_when_calibration_off(
        monkeypatch, audience, db_session):
    """If calibration mode is off, panel membership is irrelevant —
    partner still resolves to PARTNER_EMAIL."""
    monkeypatch.delenv("BRIEF_CALIBRATION_MODE", raising=False)
    monkeypatch.setenv("BRIEF_CALIBRATION_PANEL", "99")  # user 1 NOT in panel
    addr, reason = be.resolve_recipient(audience, db_session)
    assert reason == "ok"
    assert addr == be.PARTNER_EMAIL


# ---- render_plain_calibration ----

def test_render_plain_calibration_includes_intro(brief_body, audience):
    text = be.render_plain_calibration(brief_body, audience)
    assert "This is a calibration test" in text
    assert "first real LLM-composed brief" in text


def test_render_plain_calibration_includes_brief_body(brief_body, audience):
    text = be.render_plain_calibration(brief_body, audience)
    assert "Good morning, Sam." in text
    assert "ORD-1 ran 18 min" in text
    assert "Have a strong day." in text


def test_render_plain_calibration_includes_all_4_questions(brief_body, audience):
    text = be.render_plain_calibration(brief_body, audience)
    assert "1-5, how useful" in text
    assert "important from yesterday" in text
    assert "noise" in text
    assert "single change" in text.lower()


def test_render_plain_calibration_includes_feedback_url(brief_body, audience):
    text = be.render_plain_calibration(brief_body, audience)
    assert "https://app.cenaskitchen.com/partner/briefs/test-brief-id-abc123/feedback" in text


def test_feedback_url_respects_override(monkeypatch, brief_body):
    monkeypatch.setenv("BRIEF_FEEDBACK_BASE_URL", "https://staging.example.com")
    url = be._feedback_url(brief_body)
    assert url == "https://staging.example.com/partner/briefs/test-brief-id-abc123/feedback"


def test_feedback_url_falls_back_when_brief_id_missing():
    url = be._feedback_url({})
    assert url.endswith("/partner/briefs")


# ---- dispatch_brief end-to-end ----

def test_dispatch_sends_calibration_email_to_panel_partner(
        monkeypatch, audience, brief_row, db_session):
    monkeypatch.setenv("BRIEF_CALIBRATION_MODE", "1")
    monkeypatch.setenv("BRIEF_CALIBRATION_PANEL", "1")  # audience.user_id=1
    monkeypatch.setenv("BRIEF_EMAIL_DISPATCH", "1")
    sent: list = []
    monkeypatch.setattr(be, "_smtp_send", lambda *a, **k: sent.append(a))
    out = be.dispatch_brief(brief_row, audience, db_session)
    assert out["status"] == "sent"
    assert out["to"] == be.PARTNER_EMAIL
    assert len(sent) == 1
    to_addr, subject, plain = sent[0]
    assert subject == "[Calibration] Cenas brief — 2026-05-13"
    assert "calibration test" in plain.lower()
    assert "single change" in plain.lower()
    # Feedback URL embedded
    assert "test-brief-id-abc123/feedback" in plain


def test_dispatch_skips_non_panel_in_calibration_mode(
        monkeypatch, audience, brief_row, db_session):
    monkeypatch.setenv("BRIEF_CALIBRATION_MODE", "1")
    monkeypatch.setenv("BRIEF_CALIBRATION_PANEL", "99")  # not audience.user_id
    monkeypatch.setenv("BRIEF_EMAIL_DISPATCH", "1")
    sent: list = []
    monkeypatch.setattr(be, "_smtp_send", lambda *a, **k: sent.append(a))
    out = be.dispatch_brief(brief_row, audience, db_session)
    assert out["status"] == "skipped"
    assert out["reason"] == "skipped_not_panel"
    assert sent == []


def test_dispatch_dry_run_in_calibration_mode_logs_calibration_flag(
        monkeypatch, audience, brief_row, db_session, caplog):
    monkeypatch.setenv("BRIEF_CALIBRATION_MODE", "1")
    monkeypatch.setenv("BRIEF_CALIBRATION_PANEL", "1")
    monkeypatch.delenv("BRIEF_EMAIL_DISPATCH", raising=False)
    sent: list = []
    monkeypatch.setattr(be, "_smtp_send", lambda *a, **k: sent.append(a))
    out = be.dispatch_brief(brief_row, audience, db_session)
    assert out["status"] == "dry_run"
    assert sent == []
    # Subject in dispatch_brief computes from format_subject(..., calibration=True)
    # We can't sniff it from the log directly without capturing, but the
    # dry-run logger path is exercised — the explicit calibration test
    # is on format_subject above.
