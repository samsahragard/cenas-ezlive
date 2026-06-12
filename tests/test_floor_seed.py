"""Tests for the one-time default-layout seeder (ck, Sam 2026-06-12).

app/data/floor_seed_layouts.json holds the real-room layouts transcribed
from Sam's Toast Tables screenshots; _bootstrap_layout_if_empty applies a
location's seed on first touch (zero floor_layouts rows), matching tables
by normalized name. These tests cover the seeder mechanics with a tiny
temp seed file, plus shape/bounds sanity of the real shipped seed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("ALLOW_DEV_SECRET", "1")

from app.floor_models import (
    FloorFixture,
    FloorLayout,
    ToastTableCfg,
    ensure_floor_tables,
)
from app.models import User

ROOT = Path(__file__).resolve().parents[1]
REAL_SEED = ROOT / "app" / "data" / "floor_seed_layouts.json"

UNO_GUID = "seedtest-guid-uno"

TINY_SEED = {
    "copperfield": {
        "tables": {
            "11": {"x": 100, "y": 100, "w": 50, "h": 50, "shape": "square"},
            "61 A": {"x": 200, "y": 100, "w": 50, "h": 35, "shape": "rect"},
            "GHOST": {"x": 300, "y": 100, "w": 50, "h": 50, "shape": "square"},
        },
        "fixtures": [
            {"type": "label", "x": 10, "y": 10, "w": 80, "h": 40, "label": "HOST"},
        ],
    }
}


@pytest.fixture
def seed_app(db_session, monkeypatch, tmp_path):
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_COPPERFIELD", UNO_GUID)

    from app import create_app
    from app import db as appdb
    from app.web import floor_routes

    monkeypatch.setattr(appdb, "SessionLocal", lambda: db_session)

    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps(TINY_SEED), encoding="utf-8")
    monkeypatch.setattr(floor_routes, "_SEED_PATH", str(seed_file))

    ensure_floor_tables(db_session.get_bind())
    # Tables already synced (so the sync bootstrap is a no-op): note the
    # DB name carries the space, the seed is matched normalized.
    db_session.add_all([
        ToastTableCfg(guid="st-11", location_guid=UNO_GUID, name="11"),
        ToastTableCfg(guid="st-61a", location_guid=UNO_GUID, name="61 A"),
        ToastTableCfg(guid="st-other", location_guid=UNO_GUID, name="99"),
    ])
    user = User(
        id=7301, full_name="Seed Partner", email="seed@test.local",
        phone="5557770001", passcode_hash="x", permission_level="partner",
        store_scope=None, active=True, first_login_done=True,
        session_version=1,
    )
    db_session.add(user)
    db_session.commit()

    flask_app = create_app()
    if "floor" not in flask_app.blueprints:
        flask_app.register_blueprint(floor_routes.floor_bp)
    flask_app.config["TESTING"] = True

    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess["auth_ok"] = True
        sess["user_id"] = user.id
        sess["user_session_version"] = user.session_version
        sess["active_store"] = "copperfield"
    return client, db_session


def _layouts(db):
    return db.query(FloorLayout).filter(
        FloorLayout.location_guid == UNO_GUID).all()


def test_seed_applies_once_and_matches_normalized_names(seed_app):
    client, db = seed_app
    resp = client.get("/floor/api/floor?loc=uno")
    assert resp.status_code == 200
    data = resp.get_json()

    placed = {t["guid"]: t for t in data["tables"]}
    assert set(placed) == {"st-11", "st-61a"}  # GHOST skipped, 99 unseeded
    assert placed["st-61a"]["shape"] == "rect"
    assert placed["st-61a"]["x"] == 200
    assert {t["guid"] for t in data["unplaced"]} == {"st-other"}
    assert len(data["fixtures"]) == 1
    assert data["fixtures"][0]["label"] == "HOST"

    # Second hit: still exactly the same rows (no duplicates, no re-seed).
    client.get("/floor/api/floor?loc=uno")
    assert len(_layouts(db)) == 2
    assert db.query(FloorFixture).filter(
        FloorFixture.location_guid == UNO_GUID).count() == 1


def test_seed_blocked_by_existing_layout(seed_app):
    client, db = seed_app
    db.add(FloorLayout(location_guid=UNO_GUID, table_guid="st-other",
                       x=1, y=2, w=3, h=4, shape="square", rotation=0))
    db.commit()
    resp = client.get("/floor/api/floor?loc=uno")
    assert resp.status_code == 200
    rows = _layouts(db)
    assert len(rows) == 1  # only the pre-existing manual row
    assert rows[0].table_guid == "st-other"


def test_real_seed_file_is_sane():
    seed = json.loads(REAL_SEED.read_text(encoding="utf-8"))
    assert set(seed) >= {"tomball", "copperfield"}
    for key in ("tomball", "copperfield"):
        tables = seed[key]["tables"]
        assert len(tables) >= 14
        norm = ["".join(n.split()).upper() for n in tables]
        assert len(set(norm)) == len(norm), f"duplicate normalized name in {key}"
        for name, row in tables.items():
            assert row["shape"] in ("square", "rect", "circle", "diamond"), name
            assert 0 <= row["x"] and row["x"] + row["w"] <= 1000, name
            assert 0 <= row["y"] and row["y"] + row["h"] <= 620, name
        for f in seed[key]["fixtures"]:
            assert f["type"] in ("wall", "label")
            assert 0 <= f["x"] and f["x"] + f["w"] <= 1000
            assert 0 <= f["y"] and f["y"] + f["h"] <= 620


def test_real_seed_names_resolve_against_synced_db():
    """Every seed name must exist in the locally synced toast_tables for its
    location (the live sync ran at Gate 2 into the worktree dev DB). Skips
    when that DB is absent (CI)."""
    dev_db = ROOT / "dev_local.db"
    if not dev_db.exists():
        pytest.skip("no local dev DB with synced Toast config")
    import sqlite3
    con = sqlite3.connect(str(dev_db))
    try:
        rows = con.execute(
            "select location_guid, name from toast_tables where deleted=0"
        ).fetchall()
    finally:
        con.close()
    if not rows:
        pytest.skip("dev DB has no synced toast_tables")
    by_loc: dict[str, set] = {}
    for lg, name in rows:
        by_loc.setdefault(lg, set()).add("".join(str(name).split()).upper())
    seed = json.loads(REAL_SEED.read_text(encoding="utf-8"))
    for key in ("tomball", "copperfield"):
        names = {"".join(n.split()).upper() for n in seed[key]["tables"]}
        # the location whose synced names best cover the seed is its store
        best = max(by_loc.values(), key=lambda s: len(s & names))
        unmatched = names - best
        assert not unmatched, f"{key}: seed names not in synced DB: {unmatched}"
