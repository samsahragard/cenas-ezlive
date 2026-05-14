"""Block 1J — the AmbientSignal data plane.

Phase 2 / Block 1J (samai spec, 2026-05-14). The in-app data-plane /
control-plane separation: six per-source /cron/refresh-* crons (Day 2)
WRITE AmbientSignal rows through ambient_signal_upsert(); the 1C
ribbon router + the /cron/sales-insights pipeline READ them. One
producer table, many consumers.

Day 1 (this module) ships the foundation:
  - _ambient_payload_hash() — the canonical change-detector hash (§2.1)
  - ambient_signal_upsert() — the shared id-stable upsert helper (§3)

The id-stable contract (spec §2.2): a re-pull of the same logical
signal — same (source, signal_key) — with a fresh payload UPDATES the
existing row IN PLACE; its id never changes. That stability is what
makes a user's RibbonItemDismissal survive a payload refresh (spec §6,
"the critical invariant").

Pure service module: imports app.models + stdlib only, no Flask.
Import-safe — the Day-2 per-source crons import it freely.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from app.models import (
    AmbientSignal,
    _VALID_AMBIENT_CATEGORIES,
    _VALID_AMBIENT_SEVERITIES,
    _VALID_AMBIENT_SOURCES,
    _VALID_AMBIENT_STORE_SCOPES,
)


def _ambient_payload_hash(payload: dict) -> str:
    """sha256 of a CANONICAL serialization of ``payload`` — the change
    detector (spec §2.1).

    Canonical (sort_keys + tight separators) so semantically-identical
    payloads hash identically: dict ordering can never cause a spurious
    "changed". All six per-source crons hash through this one function,
    so their hashes are directly comparable.

    The hash covers the payload CONTENT only — row metadata
    (id / last_seen_at / updated_at) lives on the row, not in
    ``payload``, so it is correctly excluded. A non-JSON-serializable
    payload raises (TypeError) — that is a cron bug and the caller's
    per-signal try/except records + skips it (spec §8).
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ambient_signal_upsert(db, *, source: str, signal_key: str,
                          payload: dict, store_scope: str, category: str,
                          severity: str, valid_until_at: datetime) -> str:
    """Upsert one logical ambient signal. Returns ``"created"`` |
    ``"updated"`` | ``"unchanged"`` (spec §2.2). Does NOT commit — the
    calling cron owns the transaction (same split as
    run_escalation_scan / lifecycle.detect_no_shows).

    The three cases, by ``(source, signal_key)`` lookup:
      - no row              -> INSERT a new row,         return "created"
      - row, hash unchanged -> bump last_seen_at only,   return "unchanged"
      - row, hash changed   -> IN-PLACE UPDATE of the    return "updated"
                               SAME row — id NEVER changes

    The "updated" case keeping the same ``id`` is the single most
    important property in 1J: it is what makes the dismissal-survival
    invariant (§6) hold — a RibbonItemDismissal references
    ``(item_type, item_id)``, so a stable id keeps the refreshed signal
    dismissed.

    Validates ``source`` / ``category`` / ``store_scope`` / ``severity``
    against the ``_VALID_AMBIENT_*`` constants; a bad value raises
    ``ValueError`` — the caller's per-signal try/except records it and
    moves on, so one bad signal never aborts the whole cron run (§8).
    """
    if source not in _VALID_AMBIENT_SOURCES:
        raise ValueError(f"bad ambient source {source!r}")
    if category not in _VALID_AMBIENT_CATEGORIES:
        raise ValueError(f"bad ambient category {category!r}")
    if store_scope not in _VALID_AMBIENT_STORE_SCOPES:
        raise ValueError(f"bad ambient store_scope {store_scope!r}")
    if severity not in _VALID_AMBIENT_SEVERITIES:
        raise ValueError(f"bad ambient severity {severity!r}")

    new_hash = _ambient_payload_hash(payload)
    now = datetime.utcnow()

    row = (db.query(AmbientSignal)
           .filter(AmbientSignal.source == source,
                   AmbientSignal.signal_key == signal_key)
           .first())

    # --- created: no row for this logical identity ---
    if row is None:
        db.add(AmbientSignal(
            source=source, signal_key=signal_key, payload=payload,
            payload_hash=new_hash, store_scope=store_scope,
            category=category, severity=severity,
            valid_until_at=valid_until_at,
            created_at=now, updated_at=now, last_seen_at=now,
        ))
        return "created"

    # --- unchanged: same payload hash — NO-OP on content ---
    if row.payload_hash == new_hash:
        # Bump last_seen_at only, so an unchanged-but-still-live signal
        # is not mistaken for stale.
        row.last_seen_at = now
        return "unchanged"

    # --- updated: hash changed — IN-PLACE UPDATE, id never changes ---
    row.payload = payload
    row.payload_hash = new_hash
    row.store_scope = store_scope
    row.category = category
    row.severity = severity
    row.valid_until_at = valid_until_at
    row.updated_at = now
    row.last_seen_at = now
    return "updated"
