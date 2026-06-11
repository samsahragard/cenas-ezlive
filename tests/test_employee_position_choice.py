"""Position-profile chooser (Sam 2026-06-10, the Sofia expo/busser case).

An employee holding 2+ positions must be ASKED which role they're working as
on login; the pick drives the dashboard header/profile. Single-position
employees go straight through. Login always re-asks (session keys popped).
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------- static layer

def test_chooser_template_exists_and_posts_position_id():
    html = _read("app/templates/employee_position_choice.html")
    assert 'name="position_id"' in html
    assert "{{ select_url }}" in html
    assert "what are you working as today?" in html


def test_routes_registered_in_employee_auth():
    src = _read("app/web/employee_auth.py")
    assert '"/employee/select-position"' in src
    assert "employee_position_choice.html" in src
    # fresh logins must re-ask: both login + logout pop the position keys
    assert src.count('"active_position_id"') >= 3


def test_dashboard_defaults_to_week_range():
    src = _read("app/web/employee_auth.py")
    assert 'request.args.get("range") or "week"' in src


# ------------------------------------------------------------ functional layer

@pytest.fixture
def emp_with_positions(db_session, monkeypatch):
    """Employee 56-alike with Busser + Expo at tomball; SessionLocal patched
    so the auth helpers read this in-memory DB."""
    from app.models import Employee, EmployeePosition, Position
    import app.web.employee_auth as ea

    emp = Employee(id=56, full_name="Sofia Hernandez Castro", active=True,
                   session_version=1)
    busser = Position(id=4, name="Busser")
    expo = Position(id=17, name="Expo")
    db_session.add_all([
        emp, busser, expo,
        EmployeePosition(id=1, employee_id=56, position_id=4, store_key="tomball"),
        EmployeePosition(id=2, employee_id=56, position_id=17, store_key="tomball"),
        # same position held at the OTHER store (prod's 'Prep, Prep' shape) -
        # the unscoped chooser must show it once
        EmployeePosition(id=3, employee_id=56, position_id=17, store_key="copperfield"),
    ])
    db_session.commit()

    class _SL:
        def __call__(self):
            return db_session
    monkeypatch.setattr(ea, "SessionLocal", lambda: db_session)
    # the fixture session must survive the helper's .close()
    monkeypatch.setattr(db_session, "close", lambda: None)
    return emp


def test_positions_helper_dedupes_and_scopes(emp_with_positions):
    from app.web.employee_auth import _employee_positions_for
    got = _employee_positions_for(56, store_key="tomball")
    assert [p["name"] for p in got] == ["Busser", "Expo"]
    # copperfield: only the Expo row lives there
    assert [p["name"] for p in _employee_positions_for(56, store_key="copperfield")] == ["Expo"]
    # unscoped: Expo held at two stores still shows ONCE
    assert [p["name"] for p in _employee_positions_for(56)] == ["Busser", "Expo"]


def test_establish_session_pops_position_keys():
    src = _read("app/web/employee_auth.py")
    establish = src.split("def _establish_employee_session", 1)[1].split("def ", 1)[0]
    assert "active_position_id" in establish
    assert "active_position_name" in establish
