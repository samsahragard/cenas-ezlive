"""Smoke test for the location-scoped Team Roster page (wave 4 S7/S8).

Verifies, against a real app render with data seeded through the proven +Add
endpoint:
  1. /uno/team and /dos/team render (200) with the 4 tabs, the per-store
     Schedule/Market iframes (S7), and the section-scoped +Add wiring (S8).
  2. The /<store>/schedules-v2/team-roster JSON the page's JS consumes is
     section-grouped per store (management[] / hourly[]) -- the backend
     contract the template depends on.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_DEV_SECRET", "1")

import pytest
from werkzeug.security import generate_password_hash

from app.models import Position, User


@pytest.fixture
def app_with_partner(db_session, monkeypatch):
    """Flask app bound to the in-memory session, logged in as a partner.
    Mirrors tests/test_section_placement.py."""
    from app import create_app
    from app import db as appdb
    from app.web import schedules_v2 as sv2_mod
    from app.web import schedules_v2_roster as roster_mod
    from app.web import employee_setup as setup_mod
    from app.web import permissions as perm_mod
    from app.web import store_routes as store_mod

    partner = User(id=1, full_name="test partner", email="partner@test.local",
                   passcode_hash=generate_password_hash("12345"),
                   permission_level="partner", store_scope=None, active=True,
                   first_login_done=True, session_version=1)
    db_session.add(partner)
    for nm in ["GM", "KM", "Server", "Cook"]:
        db_session.add(Position(name=nm, store_key=None))
    db_session.commit()

    flask_app = create_app()
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    sess = lambda: db_session
    for mod in (appdb, sv2_mod, roster_mod, setup_mod, perm_mod, store_mod):
        if hasattr(mod, "SessionLocal"):
            monkeypatch.setattr(mod, "SessionLocal", sess, raising=False)
    import app.services.brief_email as be
    monkeypatch.setattr(be, "_smtp_send", lambda *a, **k: None, raising=False)
    return flask_app, db_session


def _partner_client(flask_app):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["partner_auth_ok"] = True
        s["auth_ok"] = True
        s["user_id"] = 1
        s["user_session_version"] = 1
    return c


def _pid(db, name):
    return db.query(Position).filter(Position.name == name).first().id


def _add(client, db, slug, store_key, full_name, pos_name, section):
    first = full_name.split()[0].lower()
    return client.post(
        f"/{slug}/schedules-v2/employees/add",
        json={"full_name": full_name, "email": f"{first}@t.local",
              "store_keys": [store_key], "position_ids": [_pid(db, pos_name)],
              "section": section})


def test_team_pages_render_and_roster_is_section_grouped(app_with_partner):
    flask_app, db = app_with_partner
    client = _partner_client(flask_app)

    # Seed through the real +Add path (one management + one hourly per store).
    assert _add(client, db, "dos", "tomball", "Gina GM", "GM", "management").status_code == 200
    assert _add(client, db, "dos", "tomball", "Sam Server", "Server", "hourly").status_code == 200
    assert _add(client, db, "uno", "copperfield", "Kara KM", "KM", "management").status_code == 200
    assert _add(client, db, "uno", "copperfield", "Cody Cook", "Cook", "hourly").status_code == 200

    # 1) Both team pages render (shell + S7 frames + S8 wiring).
    for slug in ("uno", "dos"):
        r = client.get(f"/{slug}/team")
        assert r.status_code == 200, r.get_data(as_text=True)[:500]
        html = r.get_data(as_text=True)
        for sub in ("team", "schedule", "market", "link"):
            assert f'data-sub="{sub}"' in html, f"missing tab {sub} on /{slug}/team"
        for key in ("week-uno", "week-dos", "market-uno", "market-dos"):
            assert key in html, f"missing per-store frame {key} on /{slug}/team"
        assert "data-add-section" in html, f"missing section +Add wiring on /{slug}/team"
        assert "openAddFor" in html

    # 2) team-roster JSON is section-grouped per store.
    rj = client.get("/dos/schedules-v2/team-roster")
    assert rj.status_code == 200, rj.get_data(as_text=True)[:500]
    data = rj.get_json()
    stores = {s["store_key"]: s for s in data["stores"]}
    assert "tomball" in stores and "copperfield" in stores, list(stores)
    for sk in ("tomball", "copperfield"):
        assert "management" in stores[sk] and "hourly" in stores[sk], stores[sk].keys()
        assert len(stores[sk]["management"]) >= 1, f"{sk} management empty"
        assert len(stores[sk]["hourly"]) >= 1, f"{sk} hourly empty"

    txt = rj.get_data(as_text=True)
    for nm in ("Gina GM", "Sam Server", "Kara KM", "Cody Cook"):
        assert nm in txt, f"{nm} missing from team-roster json"
