"""SA-1 self-tests for app/services/toast_config_sync.py (no network).

Covers the contract section 10 SA-1 behaviors:
- upsert new rows / update changed rows
- soft-delete ONLY on full pulls; incremental never soft-deletes
- high-water mark (floor_sync_state) advances
- re-run idempotency (0 upserts / 0 soft-deletes on no upstream change)
- revival (deleted row returned again -> deleted=0)
- lastModified rejection -> permanent full-pull fallback (UNSUPPORTED)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.floor_models import (
    FloorSyncState,
    ToastServiceArea,
    ToastTableCfg,
    ensure_floor_tables,
)
from app.services import toast_config_sync as sync_mod
from app.services.toast_client import ToastError

LOC_KEY = "copperfield"
LOC_GUID = "test-rest-guid-copperfield"
LOC_KEY_2 = "tomball"
LOC_GUID_2 = "test-rest-guid-tomball"


# ---------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_COPPERFIELD", LOC_GUID)
    monkeypatch.setenv("TOAST_RESTAURANT_GUID_TOMBALL", LOC_GUID_2)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    ensure_floor_tables(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.rollback()
        sess.close()
        engine.dispose()


class FakeToast:
    """Stands in for ToastClient: serves canned config entities."""

    def __init__(self):
        self.tables: list[dict] = []
        self.service_areas: list[dict] = []
        self.changed_tables: list[dict] = []
        self.changed_service_areas: list[dict] = []
        self.reject_last_modified = False
        self.calls: list[tuple] = []

    def fetch_tables(self, location, restaurant_guid, refresh=False):
        self.calls.append(("fetch_tables", location, refresh))
        return [dict(e) for e in self.tables]

    def fetch_service_areas(self, location, restaurant_guid, refresh=False):
        self.calls.append(("fetch_service_areas", location, refresh))
        return [dict(e) for e in self.service_areas]

    def fetch_config_since(self, resource, restaurant_guid, last_modified):
        self.calls.append(("fetch_config_since", resource, last_modified))
        if self.reject_last_modified:
            raise ToastError(
                f"Toast HTTP 400 for /config/v2/{resource}: bad lastModified"
            )
        if resource == "tables":
            return [dict(e) for e in self.changed_tables]
        return [dict(e) for e in self.changed_service_areas]


@pytest.fixture
def client():
    fake = FakeToast()
    fake.tables = [tbl("t1", "1"), tbl("t2", "2"), tbl("t3", "3")]
    fake.service_areas = [area("a1", "Dining Room"), area("a2", "Patio")]
    return fake


def tbl(guid, name, sa="a1", rc="rc1", deleted=None):
    e = {
        "guid": guid,
        "name": name,
        "serviceArea": {"guid": sa},
        "revenueCenter": {"guid": rc},
    }
    if deleted is not None:
        e["deleted"] = deleted
    return e


def area(guid, name, deleted=None):
    e = {"guid": guid, "name": name}
    if deleted is not None:
        e["deleted"] = deleted
    return e


def run(session, client, key=LOC_KEY, **kw):
    return sync_mod.sync_location(key, client=client, session=session, **kw)


# ------------------------------------------------------------------- tests
def test_first_run_full_pull_upserts_new_rows(session, client):
    counts = run(session, client)
    assert counts == {
        "tables_upserted": 3,
        "tables_soft_deleted": 0,
        "service_areas_upserted": 2,
        "service_areas_soft_deleted": 0,
        "source": "full",
    }
    rows = {r.guid: r for r in session.query(ToastTableCfg).all()}
    assert set(rows) == {"t1", "t2", "t3"}
    r = rows["t1"]
    assert r.location_guid == LOC_GUID
    assert r.name == "1"
    assert r.service_area_guid == "a1"
    assert r.revenue_center_guid == "rc1"
    assert r.deleted is False
    assert r.last_synced is not None
    sa_rows = {r.guid: r for r in session.query(ToastServiceArea).all()}
    assert set(sa_rows) == {"a1", "a2"}
    assert sa_rows["a1"].location_guid == LOC_GUID
    # full pull must bypass the disk cache (fresh data)
    assert ("fetch_tables", LOC_KEY, True) in client.calls
    assert ("fetch_service_areas", LOC_KEY, True) in client.calls


def test_second_run_is_incremental_and_idempotent(session, client):
    run(session, client)
    client.calls.clear()
    counts = run(session, client)  # no upstream change
    assert counts == {
        "tables_upserted": 0,
        "tables_soft_deleted": 0,
        "service_areas_upserted": 0,
        "service_areas_soft_deleted": 0,
        "source": "incremental",
    }
    # incremental path used, full fetchers untouched
    names = [c[0] for c in client.calls]
    assert names == ["fetch_config_since", "fetch_config_since"]


def test_incremental_updates_changed_rows(session, client):
    run(session, client)
    client.changed_tables = [tbl("t2", "2-renamed", sa="a2")]
    counts = run(session, client)
    assert counts["tables_upserted"] == 1
    assert counts["tables_soft_deleted"] == 0
    assert counts["source"] == "incremental"
    row = session.get(ToastTableCfg, "t2")
    assert row.name == "2-renamed"
    assert row.service_area_guid == "a2"


def test_full_pull_soft_deletes_missing_rows_never_hard_deletes(session, client):
    run(session, client)
    client.tables = [tbl("t1", "1"), tbl("t3", "3")]  # t2 gone upstream
    client.service_areas = [area("a1", "Dining Room")]  # a2 gone
    counts = run(session, client, force_full=True)
    assert counts["source"] == "full"
    assert counts["tables_soft_deleted"] == 1
    assert counts["service_areas_soft_deleted"] == 1
    assert counts["tables_upserted"] == 0
    # row still exists (soft delete), flagged deleted
    t2 = session.get(ToastTableCfg, "t2")
    assert t2 is not None and t2.deleted is True
    a2 = session.get(ToastServiceArea, "a2")
    assert a2 is not None and a2.deleted is True
    assert session.query(ToastTableCfg).count() == 3


def test_incremental_never_soft_deletes(session, client):
    run(session, client)
    # upstream: t2 deleted, but the incremental feed only reports t1 changed
    client.changed_tables = [tbl("t1", "1-new")]
    counts = run(session, client)
    assert counts["tables_soft_deleted"] == 0
    assert counts["service_areas_soft_deleted"] == 0
    assert session.get(ToastTableCfg, "t2").deleted is False
    # even an explicit upstream tombstone must not soft-delete incrementally
    client.changed_tables = [tbl("t3", "3", deleted=True)]
    counts = run(session, client)
    assert counts["tables_soft_deleted"] == 0
    assert counts["tables_upserted"] == 0
    assert session.get(ToastTableCfg, "t3").deleted is False


def test_full_pull_tombstone_entity_is_soft_deleted(session, client):
    run(session, client)
    client.tables = [tbl("t1", "1"), tbl("t2", "2"), tbl("t3", "3", deleted=True)]
    counts = run(session, client, force_full=True)
    assert counts["tables_soft_deleted"] == 1
    assert session.get(ToastTableCfg, "t3").deleted is True


def test_high_water_mark_advances(session, client, monkeypatch):
    t0 = datetime(2026, 6, 11, 12, 0, 0)
    monkeypatch.setattr(sync_mod, "_utcnow", lambda: t0)
    run(session, client)
    states = {
        s.resource: s
        for s in session.query(FloorSyncState).filter_by(location_guid=LOC_GUID)
    }
    assert set(states) == {"tables", "service_areas"}
    first = states["tables"].last_modified
    assert first == "2026-06-11T11:55:00.000+0000"  # t0 - 5 min overlap
    assert states["tables"].last_run_at == t0

    monkeypatch.setattr(sync_mod, "_utcnow", lambda: t0 + timedelta(hours=2))
    run(session, client)
    session.expire_all()
    state = (
        session.query(FloorSyncState)
        .filter_by(location_guid=LOC_GUID, resource="tables")
        .one()
    )
    assert state.last_modified == "2026-06-11T13:55:00.000+0000"
    assert state.last_modified > first
    # the incremental pull was made with the PREVIOUS watermark
    inc_calls = [c for c in client.calls if c[0] == "fetch_config_since"]
    assert inc_calls and all(c[2] == first for c in inc_calls)


def test_rerun_idempotency_full(session, client):
    run(session, client)
    counts = run(session, client, force_full=True)  # same upstream data
    assert counts == {
        "tables_upserted": 0,
        "tables_soft_deleted": 0,
        "service_areas_upserted": 0,
        "service_areas_soft_deleted": 0,
        "source": "full",
    }


def test_revival_sets_deleted_back_to_zero(session, client):
    run(session, client)
    client.tables = [tbl("t1", "1"), tbl("t2", "2")]  # t3 vanishes
    run(session, client, force_full=True)
    assert session.get(ToastTableCfg, "t3").deleted is True
    # t3 comes back via incremental
    client.changed_tables = [tbl("t3", "3")]
    counts = run(session, client)
    assert counts["tables_upserted"] == 1
    assert counts["tables_soft_deleted"] == 0
    assert session.get(ToastTableCfg, "t3").deleted is False


def test_lastmodified_rejected_falls_back_to_full_permanently(session, client):
    run(session, client)
    client.reject_last_modified = True
    counts = run(session, client)
    assert counts["source"] == "full"  # fell back within the same run
    states = {
        s.resource: s.last_modified
        for s in session.query(FloorSyncState).filter_by(location_guid=LOC_GUID)
    }
    assert states == {
        "tables": sync_mod.UNSUPPORTED,
        "service_areas": sync_mod.UNSUPPORTED,
    }
    # next run must not even try lastModified again
    client.reject_last_modified = False
    client.calls.clear()
    counts = run(session, client)
    assert counts["source"] == "full"
    assert all(c[0] != "fetch_config_since" for c in client.calls)


def test_non_400_toast_error_propagates_and_rolls_back(session, client):
    run(session, client)

    def boom(resource, restaurant_guid, last_modified):
        raise ToastError("Toast HTTP 503 for /config/v2/tables: down")

    client.fetch_config_since = boom
    before = session.query(ToastTableCfg).count()
    with pytest.raises(ToastError):
        run(session, client)
    assert session.query(ToastTableCfg).count() == before
    assert all(r.deleted is False for r in session.query(ToastTableCfg))


def test_sync_all_covers_both_locations(session, client):
    # real Toast guids are globally unique - namespace the fake's per location
    client.fetch_tables = lambda location, guid, refresh=False: [
        tbl(f"{location}-t{i}", str(i)) for i in (1, 2, 3)
    ]
    client.fetch_service_areas = lambda location, guid, refresh=False: [
        area(f"{location}-a1", "Dining Room")
    ]
    result = sync_mod.sync_all(client=client, session=session)
    assert set(result) == {LOC_KEY, LOC_KEY_2}
    for counts in result.values():
        assert counts["source"] == "full"
        assert counts["tables_upserted"] == 3
    # rows are scoped per location_guid
    locs = {
        r.location_guid for r in session.query(ToastTableCfg).all()
    }
    assert locs == {LOC_GUID, LOC_GUID_2}


def test_unknown_location_key_raises(session, client):
    with pytest.raises(ValueError):
        run(session, client, key="nowhere")
