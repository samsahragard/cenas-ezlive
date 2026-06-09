"""Cenas Floor OS - end-to-end smoke for the 5 employee tabs.

For each of (Today, Tables, Shifts, Inbox, You) we:
1. Render the route through the Flask test client with a logged-in employee.
2. Assert the response is 200.
3. Assert Floor OS markers are in the HTML (cf-* classes from the visual
   system, the demo-mode source label, etc).
4. Assert the existing session/redirect guard still bounces logged-out
   callers to /employee/login (not the staff keypad).

The fixture builds an isolated SQLite DB so this test file does not leak into
the other employee suites in the same pytest process.
"""
import os
import tempfile

import pytest


@pytest.fixture()
def app_emp():
    tmp = os.path.join(tempfile.gettempdir(), "_floor_os_smoke.db")
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    os.environ["ALLOW_DEV_SECRET"] = "1"
    os.environ["DATABASE_URL"] = "sqlite:///" + tmp.replace("\\", "/")
    from app import create_app
    app = create_app()
    from app.db import SessionLocal
    from app.models import Employee
    db = SessionLocal()
    e = Employee(full_name="Kennya Garcia", active=True, session_version=1, passcode_hash="x")
    db.add(e)
    db.commit()
    eid = e.id
    db.close()
    yield app, eid
    try:
        os.remove(tmp)
    except OSError:
        pass


def _login(client, eid):
    with client.session_transaction() as s:
        s.clear()
        s["employee_id"] = eid
        s["auth_ok"] = True


def _assert_floor_os_shell(html: str):
    """Every Floor OS tab must wear the shared chrome + visual layer."""
    assert "employee_console.css" in html
    assert 'class="cc-shell"' in html or "cc-shell" in html
    assert "cc-bottom-tabs" in html
    # Five tabs are wired by the shell, with the active one marked.
    assert html.count('class="cc-tab') == 5
    assert 'aria-current="page"' in html


def test_today_tab_resets_to_zero_by_date(app_emp):
    """Floor Pulse V2: Today is date-true. With no checks posted for the real
    local date, the hero take-home is $0.00 and the empty state shows. Yesterday
    must not roll forward into Today."""
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/dashboard")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_floor_os_shell(html)
    # V2 hero + date-reset markers
    assert "cfp-hero-money" in html
    assert "$0.00" in html
    assert "Nothing posted for" in html
    assert "Yesterday will not roll forward" in html
    # Today range shows the empty peer ranking, not a fabricated board.
    assert "No ranked shift yet" in html
    assert "Demo mode" in html


def test_today_week_range_shows_stats_and_ranking_no_table_rail(app_emp):
    """Week/Month/Last30: stats + technical averages + peer ranking, and NO
    table map / ticket rail."""
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/dashboard?range=week")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_floor_os_shell(html)
    # Stats present
    assert "Technical averages" in html
    assert "Peer ranking" in html
    assert "Rank #" in html  # the employee's own rank chip
    assert "Kennya Garcia" in html  # leaderboard rows
    # The range note replaces the live table rail
    assert "No table rail on week view" in html
    # No today-only zero state on a populated range
    assert "Nothing posted for" not in html


def test_today_hero_cards_link_to_sections(app_emp):
    """Clickable hero cells must target the on-page section anchors."""
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    html = c.get("/employee/dashboard?range=week").get_data(as_text=True)
    assert 'href="#cfp-earnings"' in html
    assert 'href="#cfp-leaderboard"' in html
    assert 'href="#cfp-technical"' in html
    assert 'id="cfp-earnings"' in html
    assert 'id="cfp-leaderboard"' in html
    assert 'id="cfp-technical"' in html


def test_tables_today_empty_yesterday_has_rail(app_emp):
    """Tables: Today is empty (date reset) with a jump to Yesterday; Yesterday
    carries the ticket rail and table tiles that open their ticket."""
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    today = c.get("/employee/tables").get_data(as_text=True)
    assert "No tables for today" in today
    assert "View yesterday tickets" in today

    yest = c.get("/employee/tables?day=yesterday").get_data(as_text=True)
    assert "cfp-table-map" in yest
    assert "cfp-ticket-list" in yest
    # Table tile and its matching ticket anchor both exist for table 62B
    assert 'data-table="62B"' in yest
    assert 'id="cfp-ticket-62B"' in yest


def test_tables_deeplink_table_selects_and_keeps_filter_safe(app_emp):
    """A ?table= deep-link (from Best Next Move / Right Now) marks the ticket
    selected, and is not orphaned by a filter that would hide it."""
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    # table 63 is NOT owner (owner=False) so filter=mine would hide it; the
    # route must fall back so the deep-linked ticket stays visible + selected.
    html = c.get("/employee/tables?day=yesterday&filter=mine&table=63").get_data(as_text=True)
    assert 'id="cfp-ticket-63"' in html
    assert "is-selected" in html


def test_shifts_tab_renders_floor_os(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/my-schedule")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_floor_os_shell(html)
    # Segmented control + the time-off form both present
    assert 'data-seg="schedule-section"' in html
    assert 'id="cf-timeoff"' in html
    # Empty state when no shifts are populated server-side yet
    assert "No shifts posted yet" in html


def test_inbox_tab_renders_floor_os(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/messages")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_floor_os_shell(html)
    assert 'data-seg="inbox-section"' in html
    # Alerts panel must know where to call alarm-preferences
    assert "/employee/alarm-preferences" in html


def test_inbox_tab_alerts_view_starts_on_alerts_pane(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/messages?view=alerts")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # Messages pane is hidden and alerts pane is visible on direct landing
    assert 'id="cf-pane-messages" data-pane="messages" hidden' in html
    assert 'id="cf-pane-alerts" data-pane="alerts"' in html and 'data-pane="alerts" hidden' not in html


def test_you_tab_renders_floor_os(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/my-profile")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_floor_os_shell(html)
    assert "cf-profile" in html
    assert "cf-news" in html
    assert "Kennya" in html  # session employee's name reaches the hero


def test_each_tab_bounces_logged_out_caller_to_employee_login(app_emp):
    """Logged-out hit on every Floor OS tab must redirect to /employee/login,
    NOT the staff keypad. Anchored by the existing route guards."""
    app, _ = app_emp
    c = app.test_client()
    with c.session_transaction() as s:
        s.clear()
    for path in (
        "/employee/dashboard",
        "/employee/tables",
        "/employee/my-schedule",
        "/employee/messages",
        "/employee/my-profile",
    ):
        r = c.get(path)
        assert r.status_code == 302, f"{path} should redirect when logged-out, got {r.status_code}"
        assert "/employee/login" in (r.headers.get("Location") or ""), (
            f"{path} should bounce to employee login, got {r.headers.get('Location')}"
        )
