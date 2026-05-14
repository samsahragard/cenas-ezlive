"""Phase 2 / Block 1J Day 1 — AmbientSignal + ambient_signal_upsert tests.

Covers (spec §2 / §2.1 / §2.2 / §3):
  - _ambient_payload_hash: canonical (key-order-independent),
    deterministic, content-sensitive.
  - ambient_signal_upsert: the three cases — created / unchanged /
    updated — and the CRITICAL id-stable property: a changed-hash
    update keeps the SAME id (the property the whole sub-block hinges
    on, §2.2); only a new signal_key makes a new row.
  - validation: bad source / category / store_scope / severity ->
    ValueError, nothing written.
  - caller-owned transaction: the helper does not commit.
  - AmbientSignal / AmbientSignalRun model round-trip + the
    uq_ambient_signal_identity unique constraint.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import AmbientSignal, AmbientSignalRun
from app.services.ambient_signals import (
    _ambient_payload_hash,
    ambient_signal_upsert,
)

_VU = datetime(2026, 5, 20, 23, 59, 0)   # a valid_until_at fixture


def _upsert(db, **over):
    kw = dict(
        source="weather",
        signal_key="tomball:forecast:2026-05-14",
        payload={"headline": "95F and humid", "high": 95},
        store_scope="both", category="maintenance", severity="info",
        valid_until_at=_VU,
    )
    kw.update(over)
    return ambient_signal_upsert(db, **kw)


# ============================================================
# _ambient_payload_hash — the change detector (§2.1)
# ============================================================

def test_payload_hash_is_canonical_key_order_independent():
    a = _ambient_payload_hash({"b": 2, "a": 1, "c": [3, 4]})
    b = _ambient_payload_hash({"c": [3, 4], "a": 1, "b": 2})
    assert a == b


def test_payload_hash_deterministic():
    p = {"headline": "x", "n": 7}
    assert _ambient_payload_hash(p) == _ambient_payload_hash(p)


def test_payload_hash_content_sensitive():
    assert (_ambient_payload_hash({"high": 95})
            != _ambient_payload_hash({"high": 96}))


def test_payload_hash_is_sha256_hex():
    h = _ambient_payload_hash({"a": 1})
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


# ============================================================
# ambient_signal_upsert — the three cases (§2.2)
# ============================================================

def test_upsert_created(db_session):
    verdict = _upsert(db_session)
    assert verdict == "created"
    db_session.flush()
    row = db_session.query(AmbientSignal).one()
    assert row.source == "weather"
    assert row.signal_key == "tomball:forecast:2026-05-14"
    assert row.payload == {"headline": "95F and humid", "high": 95}
    assert row.payload_hash == _ambient_payload_hash(row.payload)
    assert row.category == "maintenance"
    assert row.store_scope == "both"
    assert row.last_seen_at is not None


def test_upsert_unchanged_is_noop_on_content(db_session):
    assert _upsert(db_session) == "created"
    db_session.flush()
    row = db_session.query(AmbientSignal).one()
    first_id, first_hash = row.id, row.payload_hash
    seen_before = row.last_seen_at

    # Same payload again -> unchanged.
    assert _upsert(db_session) == "unchanged"
    db_session.flush()
    row = db_session.query(AmbientSignal).one()   # still exactly one row
    assert row.id == first_id                     # id stable
    assert row.payload_hash == first_hash         # content untouched
    assert row.last_seen_at >= seen_before        # last_seen_at bumped


def test_upsert_updated_keeps_same_id(db_session):
    # THE critical property: a changed payload updates the row IN PLACE.
    assert _upsert(db_session) == "created"
    db_session.flush()
    original = db_session.query(AmbientSignal).one()
    original_id = original.id
    original_hash = original.payload_hash

    # Same (source, signal_key), CHANGED payload.
    verdict = _upsert(db_session,
                      payload={"headline": "99F — heat advisory", "high": 99},
                      severity="warn")
    assert verdict == "updated"
    db_session.flush()

    rows = db_session.query(AmbientSignal).all()
    assert len(rows) == 1                          # NO second row
    row = rows[0]
    assert row.id == original_id                   # <-- id NEVER changes
    assert row.payload_hash != original_hash       # content changed
    assert row.payload == {"headline": "99F — heat advisory", "high": 99}
    assert row.severity == "warn"


def test_upsert_new_signal_key_creates_new_row(db_session):
    assert _upsert(db_session, signal_key="k1") == "created"
    assert _upsert(db_session, signal_key="k2") == "created"
    db_session.flush()
    rows = db_session.query(AmbientSignal).order_by(AmbientSignal.id).all()
    assert len(rows) == 2
    assert rows[0].id != rows[1].id


def test_upsert_same_key_different_source_are_separate(db_session):
    # The identity is the (source, signal_key) PAIR — same key under a
    # different source is a different logical signal.
    assert _upsert(db_session, source="weather", signal_key="shared",
                   category="maintenance") == "created"
    assert _upsert(db_session, source="outages", signal_key="shared",
                   category="maintenance") == "created"
    db_session.flush()
    assert db_session.query(AmbientSignal).count() == 2


# ============================================================
# Validation (§3) — bad value -> ValueError, nothing written
# ============================================================

@pytest.mark.parametrize("field,bad", [
    ("source", "telepathy"),
    ("category", "vendor"),          # a Task category, not an ambient one
    ("store_scope", "narnia"),
    ("severity", "catastrophic"),
])
def test_upsert_validation_rejects_bad_value(db_session, field, bad):
    with pytest.raises(ValueError):
        _upsert(db_session, **{field: bad})
    db_session.flush()
    assert db_session.query(AmbientSignal).count() == 0


# ============================================================
# Caller-owned transaction — the helper does not commit (§3)
# ============================================================

def test_upsert_does_not_commit(db_session):
    assert _upsert(db_session) == "created"
    # The helper only mutates the session; the caller owns commit. A
    # rollback here must discard the row.
    db_session.rollback()
    assert db_session.query(AmbientSignal).count() == 0


# ============================================================
# Models — round-trip + the unique constraint
# ============================================================

def test_ambient_signal_run_roundtrips(db_session):
    now = datetime(2026, 5, 14, 10, 0, 0)
    run = AmbientSignalRun(
        source="weather", started_at=now, finished_at=now,
        status="success", signals_created=2, signals_updated=1,
        signals_unchanged=5, signals_expired=3,
    )
    db_session.add(run)
    db_session.commit()
    row = db_session.query(AmbientSignalRun).one()
    assert row.source == "weather"
    assert row.status == "success"
    assert (row.signals_created, row.signals_updated,
            row.signals_unchanged, row.signals_expired) == (2, 1, 5, 3)
    assert row.error_text is None


def test_uq_ambient_signal_identity_rejects_duplicate(db_session):
    base = dict(payload={"x": 1}, payload_hash="h", store_scope="both",
                category="events", severity="info", valid_until_at=_VU,
                created_at=_VU, updated_at=_VU, last_seen_at=_VU)
    db_session.add(AmbientSignal(source="events", signal_key="dup", **base))
    db_session.add(AmbientSignal(source="events", signal_key="dup", **base))
    with pytest.raises(IntegrityError):
        db_session.flush()
