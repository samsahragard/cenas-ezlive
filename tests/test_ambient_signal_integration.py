"""Phase 2 / Block 1J Day 4 — AmbientSignal cross-system integration tests (ck).

The three named §10 test contracts — integration-level, crossing the
unit boundaries that aick's Day-1/2 test_ambient_signals.py covers
(samai 1J review confirmed 06:11: integration tests cross unit
boundaries by nature; the three §10 contracts are ck's Day-4 lane):

  1. dismissal-survives-refresh — a user's RibbonItemDismissal of an
     ambient_signal ribbon item keeps hiding that item across payload
     refreshes of the same logical signal. The id-stable in-place
     update (§2.2) is what makes it hold; the changed-payload case is
     the real proof of "the critical invariant" (§6). Crosses the 1D
     dismiss handler -> ambient_signal_upsert -> 1C ribbon render.

  2. hash-change -> in-place update — a payload refresh through the
     real cron runner updates the row IN PLACE (same id, new content),
     no second row; the ribbon then renders the UPDATED signal at that
     same item_id. A genuinely new signal_key DOES create a new row.
     Crosses run_refresh_cron -> ambient_signal_upsert -> ribbon.

  3. cron-independence — three per-source crons run through the real
     run_refresh_cron; one fetch raises. The failing source writes an
     AmbientSignalRun(status="error") and zero signals; the other two
     write their signals + their own success runs, untouched. Proves
     the ISOLATION across multiple sources (§8).

Run cold — in-memory SQLite via the db_session fixture. g.current_user
is a SimpleNamespace (the dismiss handler + the router only read .id /
.permission_level / .store_scope); ambient_signal_upsert + the cron
runner take the session directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from flask import g

from app.models import AmbientSignal, AmbientSignalRun
from app.services.ambient_signals import ambient_signal_upsert, run_refresh_cron
from app.services.ribbon import ribbon_items_for
from app.web.ribbon_routes import ribbon_dismiss


# A valid_until_at safely in the future, so the ribbon's
# valid_until_at >= now read-filter — and run_refresh_cron's per-source
# expiry sweep — keep the seeded signals live for the assertions.
_VU = datetime.utcnow() + timedelta(days=2)


@pytest.fixture(scope="session")
def app():
    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


def _user(user_id=1, role="gm", store_scope="tomball"):
    return SimpleNamespace(
        id=user_id, full_name=f"Test {role}",
        permission_level=role, store_scope=store_scope, active=True,
    )


def _sig(signal_key, **over):
    """One run_refresh_cron fetch dict — the ambient_signal_upsert
    kwargs minus `source` (run_refresh_cron supplies that)."""
    base = dict(signal_key=signal_key, payload={"k": signal_key},
                store_scope="both", category="maintenance",
                severity="info", valid_until_at=_VU)
    base.update(over)
    return base


def _dismiss_via_handler(app, db, monkeypatch, item_type, item_id, user):
    """Invoke the real 1D ribbon_dismiss handler. Patches
    ribbon_routes.SessionLocal -> the shared session (the module did
    `from app.db import SessionLocal` at import, so the name lives in
    the ribbon_routes namespace). Returns the HTTP status int."""
    monkeypatch.setattr(
        "app.web.ribbon_routes.SessionLocal", lambda: db)
    with app.test_request_context(
        f"/partner/ribbon/dismiss/{item_type}/{item_id}", method="POST",
    ):
        g.current_user = user
        result = ribbon_dismiss(item_type, item_id)
    if isinstance(result, tuple):
        return result[1]
    return result.status_code


def _render(db, monkeypatch, page_slug, user, store_scope="tomball"):
    """Render the ribbon through 1C's real router, with
    ribbon.SessionLocal bound to the shared session."""
    monkeypatch.setattr(
        "app.services.ribbon.SessionLocal", lambda: db)
    return ribbon_items_for(page_slug, user, store_scope)


def _has_ambient(items, item_id):
    return any(i.item_type == "ambient_signal" and i.item_id == item_id
               for i in items)


# ============================================================
# §10 contract 1 — dismissal-survives-refresh
# ============================================================

def test_dismissal_survives_refresh_unchanged_payload(app, db_session, monkeypatch):
    """The unchanged-payload case: a user dismisses an ambient signal
    via the real 1D handler; an unchanged re-pull (ambient_signal_upsert
    -> "unchanged") leaves the id stable; the next ribbon render still
    hides it."""
    user = _user(user_id=21)
    sk = "tomball:forecast:2026-05-14"
    payload = {"headline": "Tomball: 95F and humid",
               "detail": "High 95F — delivery-heavy dinner."}
    assert ambient_signal_upsert(
        db_session, source="weather", signal_key=sk, payload=payload,
        store_scope="both", category="maintenance", severity="info",
        valid_until_at=_VU) == "created"
    db_session.commit()
    sig_id = db_session.query(AmbientSignal).filter_by(signal_key=sk).one().id

    assert _dismiss_via_handler(
        app, db_session, monkeypatch, "ambient_signal", sig_id, user) == 200

    # an UNCHANGED re-pull — the "unchanged" upsert branch
    assert ambient_signal_upsert(
        db_session, source="weather", signal_key=sk, payload=payload,
        store_scope="both", category="maintenance", severity="info",
        valid_until_at=_VU) == "unchanged"
    db_session.commit()

    # id stable + still hidden on the next render
    assert db_session.query(AmbientSignal).filter_by(
        signal_key=sk).one().id == sig_id
    items = _render(db_session, monkeypatch, "maintenance", user)
    assert not _has_ambient(items, sig_id)


def test_dismissal_survives_refresh_changed_payload(app, db_session, monkeypatch):
    """The changed-payload case — THE real proof (1J §6, "the critical
    invariant"): a payload refresh is an in-place UPDATE that preserves
    the id, so the user's RibbonItemDismissal still matches and the
    refreshed signal stays hidden. A new id here would resurface a
    dismissed signal every cadence."""
    user = _user(user_id=22)
    sk = "centerpoint:area:77375"
    assert ambient_signal_upsert(
        db_session, source="outages", signal_key=sk,
        payload={"headline": "Outage: 120 customers", "detail": "Area 77375."},
        store_scope="both", category="maintenance", severity="warn",
        valid_until_at=_VU) == "created"
    db_session.commit()
    sig_id = db_session.query(AmbientSignal).filter_by(signal_key=sk).one().id

    assert _dismiss_via_handler(
        app, db_session, monkeypatch, "ambient_signal", sig_id, user) == 200

    # a CHANGED re-pull — in-place update, id preserved
    assert ambient_signal_upsert(
        db_session, source="outages", signal_key=sk,
        payload={"headline": "Outage: 1,400 customers",
                 "detail": "Area 77375 — outage growing."},
        store_scope="both", category="maintenance", severity="alert",
        valid_until_at=_VU) == "updated"
    db_session.commit()

    # the id did NOT change — that is what keeps the dismissal matching
    row = db_session.query(AmbientSignal).filter_by(signal_key=sk).one()
    assert row.id == sig_id
    assert row.payload["headline"] == "Outage: 1,400 customers"  # content refreshed

    # the dismissal still hides the refreshed signal
    items = _render(db_session, monkeypatch, "maintenance", user)
    assert not _has_ambient(items, sig_id)

    # control: a DIFFERENT user who never dismissed DOES see the refreshed
    # signal — proves "hidden" is the dismissal working, not the signal
    # simply failing to render at all.
    other = _user(user_id=999)
    other_items = _render(db_session, monkeypatch, "maintenance", other)
    assert _has_ambient(other_items, sig_id)


# ============================================================
# §10 contract 2 — hash-change -> in-place update
# ============================================================

def test_hash_change_is_in_place_update_seen_through_ribbon(
        app, db_session, monkeypatch):
    """A payload refresh through the real cron runner updates the row
    IN PLACE — same id, new payload / payload_hash / updated_at, no
    second row — and the ribbon renders the UPDATED content at that
    same item_id. (aick's b889b6f has the unit-level upsert cases; this
    crosses run_refresh_cron -> ambient_signal_upsert -> ribbon.)"""
    user = _user(user_id=3)
    sk = "centerpoint:area:77375"

    # run 1 — the cron creates the signal (hash A)
    run_refresh_cron(db_session, "outages", lambda db: [_sig(
        sk, payload={"headline": "Outage: 120 customers",
                     "detail": "CenterPoint area 77375."},
        severity="warn")])
    db_session.commit()
    row_a = db_session.query(AmbientSignal).filter_by(signal_key=sk).one()
    id_a, hash_a, updated_a = row_a.id, row_a.payload_hash, row_a.updated_at

    # run 2 — same signal_key, CHANGED payload (hash B)
    run_refresh_cron(db_session, "outages", lambda db: [_sig(
        sk, payload={"headline": "Outage: 1,400 customers",
                     "detail": "CenterPoint area 77375 — growing."},
        severity="alert")])
    db_session.commit()

    # in-place update: still ONE row, same id, refreshed content
    rows = db_session.query(AmbientSignal).filter_by(signal_key=sk).all()
    assert len(rows) == 1                       # NO second row
    row_b = rows[0]
    assert row_b.id == id_a                     # id NEVER changes (§2.2)
    assert row_b.payload_hash != hash_a         # content changed
    assert row_b.updated_at >= updated_a        # updated_at bumped
    assert row_b.payload["headline"] == "Outage: 1,400 customers"

    # the ribbon renders the UPDATED signal at the SAME item_id
    items = _render(db_session, monkeypatch, "maintenance", user)
    ambient = [i for i in items if i.item_type == "ambient_signal"]
    assert len(ambient) == 1
    assert ambient[0].item_id == id_a
    assert ambient[0].render_for(user)["text"] == "Outage: 1,400 customers"


def test_new_signal_key_creates_new_row(db_session):
    """The contract's second half: a genuinely NEW signal_key creates a
    NEW row (a different logical signal), not an in-place update — the
    boundary the dismissal-survival invariant correctly does not cross
    (§6). Run through the real cron runner."""
    run_refresh_cron(db_session, "outages", lambda db: [
        _sig("centerpoint:area:77375", payload={"h": 1}),
        _sig("centerpoint:area:77429", payload={"h": 2}),
    ])
    db_session.commit()
    rows = db_session.query(AmbientSignal).order_by(AmbientSignal.id).all()
    assert len(rows) == 2
    assert rows[0].id != rows[1].id
    assert {r.signal_key for r in rows} == {
        "centerpoint:area:77375", "centerpoint:area:77429"}


# ============================================================
# §10 contract 3 — cron-independence
# ============================================================

def test_cron_independence_one_failing_does_not_affect_others(db_session):
    """Three per-source crons run through the real run_refresh_cron —
    one fetch raises past its guard. The failing source writes an
    AmbientSignalRun(status="error") and zero signals; the other two
    write their signals + their own success runs, untouched. No
    cross-contamination (§8). (aick's b889b6f has the single-cron error
    case; this proves the ISOLATION across multiple sources.)"""
    def boom(db):
        raise RuntimeError("CenterPoint scrape down")

    s_out = run_refresh_cron(db_session, "outages", boom)
    s_wx = run_refresh_cron(db_session, "weather", lambda db: [
        _sig("tomball:forecast", payload={"t": 95})])
    s_cat = run_refresh_cron(db_session, "catering_pipeline", lambda db: [
        _sig("scheduled_event:1", payload={"e": "wedding"},
             category="caterings")])
    db_session.commit()

    # the failing cron: error status, zero signals — but its run IS recorded
    assert s_out["status"] == "error"
    assert s_out["signals_created"] == 0
    assert "CenterPoint scrape down" in s_out["error_text"]

    # the other two: completely unaffected
    assert s_wx["status"] == "success" and s_wx["signals_created"] == 1
    assert s_cat["status"] == "success" and s_cat["signals_created"] == 1

    # row-level isolation — only the healthy sources wrote signals
    by_source: dict[str, int] = {}
    for r in db_session.query(AmbientSignal).all():
        by_source[r.source] = by_source.get(r.source, 0) + 1
    assert by_source == {"weather": 1, "catering_pipeline": 1}  # no "outages"

    # one AmbientSignalRun per source, statuses isolated
    runs = {r.source: r.status
            for r in db_session.query(AmbientSignalRun).all()}
    assert runs == {"outages": "error", "weather": "success",
                    "catering_pipeline": "success"}
