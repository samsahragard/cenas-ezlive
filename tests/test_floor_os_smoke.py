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


def test_today_tab_renders_floor_os(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/dashboard")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_floor_os_shell(html)
    # Today-specific Floor OS markers
    assert "cf-hero" in html
    assert "cf-money" in html
    assert "cf-coach" in html
    assert "cf-ledger" in html
    # Honest demo-mode label
    assert "Demo mode" in html


def test_tables_tab_renders_floor_os(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/tables")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    _assert_floor_os_shell(html)
    # Tables-specific Floor OS markers
    assert "cf-map" in html
    assert "cf-rail-wrap" in html
    assert "cf-ticket" in html
    assert "cf-ticket-perf" in html
    # The day toggle ships with 'today' selected by default
    assert 'data-value="today"' in html
    # Honest source label
    assert "demo mode" in html.lower()


def test_tables_tab_day_toggle_serves_yesterday(app_emp):
    app, eid = app_emp
    c = app.test_client()
    _login(c, eid)
    r = c.get("/employee/tables?day=yesterday")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # The yesterday fixture has the y5/y6/y7 check ids
    assert "#y5" in html or "y5" in html


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
