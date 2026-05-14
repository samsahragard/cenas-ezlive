"""Phase 1 / Block 6 follow-up — brief email dispatch tests.

Asserts:
  - _count_severities tallies alerts/warns by section_kind
  - format_subject matches the spec §11 string
  - resolve_recipient: partner → PARTNER_EMAIL, others → User.email,
    missing email → None + "no_email" tag, missing user → None +
    "user_missing"
  - dispatch_brief:
      * dry-run by default (no SMTP call, status="dry_run")
      * BRIEF_EMAIL_DISPATCH=1 calls _smtp_send exactly once
      * SMTP exception → status="error", never raises
      * skipped recipient → status="skipped"
  - render_plain produces the expected line structure (greeting,
    headline, sections, closing) without crashing on empty inputs

These tests run cold (no Anthropic, no real SMTP). render_html is
exercised via the dispatch path; the actual Jinja template render
happens inside a flask app context provided by the fixture.
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.services import brief_email as be
from app.services.brief_composer import AudienceContext
from app.models import User, MorningBrief


# ---- fixtures ----

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
        "brief_id": "test-id",
        "audience_role": "partner",
        "audience_user_id": 1,
        "brief_date": "2026-05-13",
        "greeting": "Good morning, Sam.",
        "headline": "Two late deliveries and a stalled produce order.",
        "sections": [
            {
                "section_kind": "alerts",
                "heading": "Alerts",
                "intro": None,
                "items": [
                    {
                        "severity": "alert",
                        "subject_label": "Order ORD-1",
                        "rule_key": "orders.no_driver_30min_before",
                        "one_line": "ORD-1 has no driver assigned 25 min before pickup.",
                        "action": "Assign or call the driver.",
                        "store_label": "Tomball",
                        "link_path": "/partner/orders/ORD-1",
                        "badge": None,
                    },
                ],
            },
            {
                "section_kind": "warns",
                "heading": "Warns",
                "intro": None,
                "items": [
                    {
                        "severity": "warn",
                        "subject_label": "Order ORD-2",
                        "rule_key": "orders.late_delivery",
                        "one_line": "ORD-2 ran 18 min past its window.",
                        "action": "Confirm completion.",
                        "store_label": "Copperfield",
                        "link_path": "/partner/orders/ORD-2",
                        "badge": None,
                    },
                    {
                        "severity": "warn",
                        "subject_label": "Vendor: Sysco",
                        "rule_key": "vendor.invoice_overdue",
                        "one_line": "Sysco invoice 14 days past due.",
                        "action": "Pay or call Sysco.",
                        "store_label": None,
                        "link_path": "/partner/invoices/SYS",
                        "badge": None,
                    },
                ],
            },
        ],
        "closing": "Have a strong day.",
        "composer_model": "deterministic",
        "fallback_used": True,
    }


@pytest.fixture
def brief_row(brief_body):
    """A stand-in for a persisted MorningBrief row. We don't actually
    persist — dispatch only reads .body off the row."""
    return SimpleNamespace(body=brief_body, id=42)


# ---- _count_severities ----

def test_count_severities_basic(brief_body):
    counts = be._count_severities(brief_body)
    assert counts.alert == 1
    assert counts.warn == 2


def test_count_severities_empty():
    counts = be._count_severities({"sections": []})
    assert counts.alert == 0
    assert counts.warn == 0


def test_count_severities_missing_sections_key():
    counts = be._count_severities({})
    assert counts.alert == 0
    assert counts.warn == 0


# ---- format_subject ----

def test_format_subject_matches_spec(brief_body):
    counts = be._count_severities(brief_body)
    subj = be.format_subject(date(2026, 5, 13), counts)
    assert subj == "Cenas brief — 2026-05-13 — 1 alerts / 2 warns"


def test_format_subject_accepts_string_date():
    counts = be._SeverityCounts(alert=0, warn=0, info=0)
    subj = be.format_subject("2026-05-14", counts)
    assert subj == "Cenas brief — 2026-05-14 — 0 alerts / 0 warns"


# ---- resolve_recipient ----

def test_resolve_partner_uses_partner_email(audience):
    addr, reason = be.resolve_recipient(audience, db=None)
    assert reason == "ok"
    assert addr == be.PARTNER_EMAIL


def test_resolve_gm_uses_user_email(db_session, audience):
    audience.role = "gm"
    audience.user_id = 99
    u = User(
        id=99, full_name="Sara GM", email="sara@copperfield.test",
        passcode_hash="x", permission_level="gm",
        store_scope="copperfield", active=True, first_login_done=True,
    )
    db_session.add(u); db_session.commit()
    addr, reason = be.resolve_recipient(audience, db_session)
    assert reason == "ok"
    assert addr == "sara@copperfield.test"


def test_resolve_returns_none_when_user_missing(db_session, audience):
    audience.role = "gm"
    audience.user_id = 9999  # not in DB
    addr, reason = be.resolve_recipient(audience, db_session)
    assert addr is None
    assert reason == "user_missing"


def test_resolve_returns_none_when_email_blank(db_session, audience):
    audience.role = "gm"
    audience.user_id = 50
    u = User(
        id=50, full_name="No Email", email=None,
        passcode_hash="x", permission_level="gm",
        active=True, first_login_done=True,
    )
    db_session.add(u); db_session.commit()
    addr, reason = be.resolve_recipient(audience, db_session)
    assert addr is None
    assert reason == "no_email"


# ---- render_plain ----

def test_render_plain_includes_all_sections(brief_body, audience):
    text = be.render_plain(brief_body, audience)
    assert "Good morning, Sam." in text
    assert "Alerts" in text
    assert "Warns" in text
    assert "ORD-1 has no driver" in text
    assert "Have a strong day." in text


def test_render_plain_handles_empty_brief(audience):
    text = be.render_plain({"sections": []}, audience)
    # Should still produce a greeting derived from audience
    assert "Sam" in text


# ---- dispatch_brief ----

def test_dispatch_dry_run_by_default(monkeypatch, audience, brief_row, db_session):
    monkeypatch.delenv("BRIEF_EMAIL_DISPATCH", raising=False)
    sent: list = []
    monkeypatch.setattr(be, "_smtp_send", lambda *a, **k: sent.append(a))
    out = be.dispatch_brief(brief_row, audience, db_session)
    assert out["status"] == "dry_run"
    assert out["reason"] == "flag_off"
    assert out["to"] == be.PARTNER_EMAIL
    assert sent == []  # SMTP not called


def test_dispatch_sends_when_flag_on(monkeypatch, audience, brief_row, db_session):
    monkeypatch.setenv("BRIEF_EMAIL_DISPATCH", "1")
    sent: list = []
    monkeypatch.setattr(be, "_smtp_send", lambda *a, **k: sent.append(a))
    out = be.dispatch_brief(brief_row, audience, db_session)
    assert out["status"] == "sent"
    assert out["reason"] == "ok"
    assert len(sent) == 1
    # _smtp_send(to_addr, subject, plain) — plain-only since 128d8a5 +
    # html-branch cleanup; render_html stays in the module for the
    # Phase 1.5 /partner/briefs UI but is no longer called from the
    # email dispatch path.
    to_addr, subject, plain = sent[0]
    assert to_addr == be.PARTNER_EMAIL
    assert subject == "Cenas brief — 2026-05-13 — 1 alerts / 2 warns"
    assert "Good morning, Sam." in plain


def test_dispatch_skips_when_no_recipient(monkeypatch, audience, brief_row, db_session):
    monkeypatch.setenv("BRIEF_EMAIL_DISPATCH", "1")
    audience.role = "gm"
    audience.user_id = 7777   # no User row → user_missing
    sent: list = []
    monkeypatch.setattr(be, "_smtp_send", lambda *a, **k: sent.append(a))
    out = be.dispatch_brief(brief_row, audience, db_session)
    assert out["status"] == "skipped"
    assert out["reason"] == "user_missing"
    assert sent == []


def test_dispatch_swallows_smtp_error(monkeypatch, audience, brief_row, db_session):
    monkeypatch.setenv("BRIEF_EMAIL_DISPATCH", "1")

    def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(be, "_smtp_send", boom)
    out = be.dispatch_brief(brief_row, audience, db_session)
    assert out["status"] == "error"
    assert out["reason"] == "RuntimeError"
    # Critically: did NOT raise out of dispatch_brief


def test_render_html_actually_parses_template(monkeypatch, audience, brief_body):
    """Smoke test that the Jinja template at templates/email/morning_brief.html
    parses without errors and produces real HTML — the other tests mock
    render_html out, so this is the safety net that catches template
    syntax/attr issues (e.g. the section.items dict-method clash that
    Jinja resolves to the dict method, not the dict key)."""
    import os
    monkeypatch.setenv("ALLOW_DEV_SECRET", "1")
    monkeypatch.setenv("SECRET_KEY", "devkey")
    from app import create_app
    app = create_app()
    with app.app_context():
        html = be.render_html(brief_body, audience)
    assert "<html>" in html.lower() or "<body" in html.lower()
    assert "Good morning" in html
    assert "ORD-1 has no driver" in html
    # Wins section not in this brief_body — but if it were, it should render
    # And the brief_date footer should always appear
    assert "2026-05-13" in html


def test_dispatch_enabled_flag_recognized_truthy_values(monkeypatch):
    for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("BRIEF_EMAIL_DISPATCH", val)
        assert be._dispatch_enabled() is True
    for val in ("0", "false", "no", "off", "", "  "):
        monkeypatch.setenv("BRIEF_EMAIL_DISPATCH", val)
        assert be._dispatch_enabled() is False
