"""Phase 2 / Block 1H — pay masking tests.

Per the 1H spec §7:
  - the user → redact_management matrix: partner → False, every other
    role (incl corporate + legacy aliases) → True
  - store-scope guard: a Tomball GM asking for Copperfield → refused;
    partner → never refused; store-unscoped roles → guard is a no-op
  - no-dollars-leak invariant carried through: a non-partner viewer's
    management rows carry no labor_cost / hours / people_count
  - partner sees detail (the inverse)
  - labor_report is mocked/fixtured — 1H's tests verify 1H's wrapper
    logic, not Toast aggregation
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import pay_masking as pm
from app.services.pay_masking import (
    _is_partner,
    _redact_management_for,
    _store_scope_refused,
    render_labor_breakdown,
)


def _user(role, store_scope=None, uid=1):
    return SimpleNamespace(id=uid, permission_level=role,
                           store_scope=store_scope)


# ============================================================
# user → redact_management matrix (§4 / §7) — the core test
# ============================================================

_NON_PARTNER_ROLES = [
    "corporate", "gm", "km", "assistant_km", "corporate_chef",
    "prep_manager", "foh_manager", "expo", "driver",
    "cook", "server", "busser", "host", "bartender",
    # legacy aliases
    "manager", "corporate-driver",
]


def test_partner_redact_is_false():
    assert _redact_management_for(_user("partner")) is False
    assert _is_partner(_user("partner")) is True


@pytest.mark.parametrize("role", _NON_PARTNER_ROLES,
                         ids=lambda r: f"role={r}")
def test_non_partner_redact_is_true(role):
    """Every non-partner role — corporate included, legacy aliases
    included — gets redact_management=True."""
    u = _user(role)
    assert _redact_management_for(u) is True
    assert _is_partner(u) is False


def test_none_user_is_not_partner():
    assert _is_partner(None) is False
    assert _redact_management_for(None) is True


def test_unknown_role_redacts():
    """An unknown / junk permission_level → not partner → redacts
    (safe-closed — an unrecognized role never sees management pay)."""
    assert _redact_management_for(_user("not_a_real_role")) is True


# ============================================================
# Store-scope guard (§5)
# ============================================================

def test_guard_refuses_gm_out_of_store():
    """A Tomball GM asking for Copperfield → refused."""
    gm = _user("gm", store_scope="tomball")
    assert _store_scope_refused("copperfield", gm) is True


def test_guard_allows_gm_in_store():
    gm = _user("gm", store_scope="tomball")
    assert _store_scope_refused("tomball", gm) is False


def test_guard_noop_for_partner():
    """partner is store-unscoped — guard never refuses, any store."""
    p = _user("partner")
    assert _store_scope_refused("tomball", p) is False
    assert _store_scope_refused("copperfield", p) is False


def test_guard_noop_for_corporate():
    c = _user("corporate")
    assert _store_scope_refused("copperfield", c) is False


def test_guard_noop_when_no_store_requested():
    """An all-stores request (store=None) isn't guarded here — it's
    bounded by labor_report's own location handling."""
    gm = _user("gm", store_scope="tomball")
    assert _store_scope_refused(None, gm) is False


def test_render_labor_breakdown_refused_result_shape():
    """The guard short-circuits before labor_report — refused result
    has refused:True + empty rows."""
    gm = _user("gm", store_scope="tomball")
    out = render_labor_breakdown("copperfield", gm)
    assert out["refused"] is True
    assert out["rows"] == []
    assert out["total_cost"] is None


# ============================================================
# render_labor_breakdown — wrapper logic over a mocked labor_report
# ============================================================

def _fake_labor_report_result():
    """A labor_report-shaped dict with one management row + one hourly
    row. The management row mirrors labor_report's redact output shape
    when redact_management=True (detail nulled, only pct_net_sales)."""
    return {
        "rows": [
            {"title": "Kitchen Manager", "role": "boh",
             "people_count": None, "hours": None, "labor_cost": None,
             "pct_net_sales": 4.2, "pct_of_labor": None, "shifts": None,
             "people": [], "redacted": True},
            {"title": "Line Cook", "role": "boh",
             "people_count": 3, "hours": 88.0, "labor_cost": 1320.0,
             "pct_net_sales": 9.1, "pct_of_labor": 31.0, "shifts": 12,
             "people": [{"name": "X", "hours": 30, "cost": 450}]},
        ],
        "total_cost": 4200.0,
        "overall_pct": 22.5,
        "total_hours": 280.0,
        "total_shifts": 40,
    }


def test_render_labor_breakdown_passes_redact_flag(monkeypatch):
    """render_labor_breakdown derives redact from the user and passes
    it to labor_report. Capture the call to assert the flag."""
    captured = {}

    def _fake(start, end, location_filter=None, redact_management=False,
              **kw):
        captured["redact_management"] = redact_management
        captured["location_filter"] = location_filter
        return _fake_labor_report_result()

    import app.services.toast_reports as tr
    monkeypatch.setattr(tr, "labor_report", _fake)

    # non-partner → redact True
    render_labor_breakdown("tomball", _user("gm", store_scope="tomball"))
    assert captured["redact_management"] is True
    assert captured["location_filter"] == "tomball"

    # partner → redact False
    render_labor_breakdown("tomball", _user("partner"))
    assert captured["redact_management"] is False


def test_no_dollars_leak_invariant_carried_through(monkeypatch):
    """The core privacy invariant, re-asserted THROUGH 1H's wrapper:
    after render_labor_breakdown for a non-partner viewer, the
    management row carries no labor_cost / hours / people_count and
    people[] is empty. Proves 1H's wrapper doesn't accidentally
    un-redact what labor_report redacted."""
    import app.services.toast_reports as tr
    monkeypatch.setattr(
        tr, "labor_report",
        lambda *a, **kw: _fake_labor_report_result())

    out = render_labor_breakdown("tomball",
                                 _user("gm", store_scope="tomball"))
    mgmt = next(r for r in out["rows"] if r["title"] == "Kitchen Manager")
    assert mgmt["labor_cost"] is None
    assert mgmt["hours"] is None
    assert mgmt["people_count"] is None
    assert mgmt["people"] == []
    # the hourly row keeps its detail
    cook = next(r for r in out["rows"] if r["title"] == "Line Cook")
    assert cook["labor_cost"] == 1320.0


def test_render_labor_breakdown_tags_result(monkeypatch):
    """The result is tagged with refused:False + redact_management so
    callers can see what masking was applied."""
    import app.services.toast_reports as tr
    monkeypatch.setattr(
        tr, "labor_report",
        lambda *a, **kw: _fake_labor_report_result())
    out = render_labor_breakdown("tomball",
                                 _user("gm", store_scope="tomball"))
    assert out["refused"] is False
    assert out["redact_management"] is True


def test_render_labor_breakdown_swallows_labor_report_error(monkeypatch):
    """If labor_report raises (Toast API down), render_labor_breakdown
    returns an error-flagged dict rather than propagating — a Toast
    outage must not 500 the ribbon / team tab that embeds this."""
    def _boom(*a, **kw):
        raise RuntimeError("toast api down")

    import app.services.toast_reports as tr
    monkeypatch.setattr(tr, "labor_report", _boom)

    out = render_labor_breakdown("tomball",
                                 _user("gm", store_scope="tomball"))
    assert out["error"] == "RuntimeError"
    assert out["rows"] == []
    assert out["refused"] is False


def test_default_window_is_week_to_date():
    """When start/end omitted, the default window is Monday 00:00 →
    now. Assert the shape via the helper."""
    start, end = pm._default_window()
    assert start.weekday() == 0  # Monday
    assert start.hour == 0 and start.minute == 0
    assert end >= start
