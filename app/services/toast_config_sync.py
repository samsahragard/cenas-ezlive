"""Toast config sync for the Sections / Floor feature (SA-1, contract section 10).

Mirrors Toast restaurant config (dining tables + service areas) into the
local DB tables `toast_tables` / `toast_service_areas` so the floor map can
join on table GUIDs without hitting Toast per request.

Rules (docs/floor_contract.md, frozen):
- Read-only against Toast: GET config endpoints only, nothing ever writes.
- Upsert by guid. Rows Toast stops returning are SOFT-deleted (deleted=1),
  never hard-deleted - historical seatings join on old GUIDs. Rows that
  reappear are revived (deleted=0).
- ONLY a successful FULL pull may soft-delete. Incremental (lastModified)
  pulls never soft-delete.
- Incremental high-water mark per (location, resource) lives in
  floor_sync_state. If Toast rejects the lastModified query param (HTTP 400)
  the resource falls back to full pulls permanently (sentinel "UNSUPPORTED"
  stored in floor_sync_state.last_modified - a DB row, no process-local
  state, so the fallback survives restarts and multi-worker gunicorn).
- Idempotent: re-running with no upstream change reports 0 upserts and
  0 soft-deletes (counts reflect actual row changes only; last_synced is
  still refreshed on every row seen by a pull).
- No process-local state for app data: everything lands in DB rows.

Public API:
    sync_location(location_key) -> {tables_upserted, tables_soft_deleted,
        service_areas_upserted, service_areas_soft_deleted,
        source: "full"|"incremental"}
    sync_all() -> {location_key: counts}

CLI (on-demand / nightly cron safe):
    python -m app.services.toast_config_sync [copperfield|tomball|all]
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from app.floor_models import (
    FloorSyncState,
    ToastServiceArea,
    ToastTableCfg,
    ensure_floor_tables,
)
from app.services.toast_client import ToastClient, ToastError, restaurant_guids

log = logging.getLogger(__name__)

RESOURCES = ("tables", "service_areas")

# floor_sync_state.last_modified sentinel: Toast rejected the lastModified
# query param for this (location, resource) -> full pulls permanently.
UNSUPPORTED = "UNSUPPORTED"

# When advancing the high-water mark, back off this many minutes from the
# pull start time so clock skew between us and Toast cannot cause a change
# to slip between two incremental pulls. Overlap is harmless: re-returned
# unchanged entities produce 0 upserts (field-compare before write).
WATERMARK_OVERLAP_MINUTES = 5

# Toast config API lastModified format (matches the offset style used by
# the other ToastClient date params).
_TOAST_TS_FMT = "%Y-%m-%dT%H:%M:%S.000+0000"

# URL leaf under /config/v2/ per resource (for incremental pulls).
_RESOURCE_URL_LEAF = {"tables": "tables", "service_areas": "serviceAreas"}

_RESOURCE_MODEL = {"tables": ToastTableCfg, "service_areas": ToastServiceArea}


def _utcnow() -> datetime:
    """Naive UTC now (contract section 2: DateTime columns store naive UTC).
    Test seam."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt_watermark(dt: datetime) -> str:
    return dt.strftime(_TOAST_TS_FMT)


def _extract_fields(resource: str, entity: dict) -> dict:
    """Map a Toast config entity onto our model columns (display name plus
    serviceArea / revenueCenter reference GUIDs for tables)."""
    fields = {"name": entity.get("name") or ""}
    if resource == "tables":
        fields["service_area_guid"] = (entity.get("serviceArea") or {}).get("guid")
        fields["revenue_center_guid"] = (entity.get("revenueCenter") or {}).get("guid")
    return fields


def _pull_full(client, location_key: str, restaurant_guid: str, resource: str) -> list:
    """Full pull, always FRESH (refresh=True bypasses the 24h disk cache -
    sync must see current Toast config, not a stale snapshot)."""
    if resource == "tables":
        return client.fetch_tables(location_key, restaurant_guid, refresh=True) or []
    return client.fetch_service_areas(location_key, restaurant_guid, refresh=True) or []


def _is_param_rejection(err: ToastError) -> bool:
    """ToastClient._http_get raises ToastError("Toast HTTP <code> for <url>: ...").
    A 400 on a lastModified pull means the param was rejected."""
    return "Toast HTTP 400" in str(err)


def _apply(session, model, location_guid: str, entities: list, *, full: bool,
           resource: str, now: datetime) -> tuple[int, int]:
    """Upsert pulled entities; on FULL pulls soft-delete rows not returned.
    Returns (upserted, soft_deleted) counting actual row changes only."""
    existing = {
        row.guid: row
        for row in session.query(model).filter_by(location_guid=location_guid).all()
    }
    seen: set[str] = set()
    upserted = 0
    soft_deleted = 0

    for entity in entities:
        if not isinstance(entity, dict):
            continue
        guid = entity.get("guid")
        if not guid:
            continue
        if entity.get("deleted") is True:
            # Explicit upstream tombstone: treat as not-returned. On a full
            # pull the missing-row rule below soft-deletes it; on an
            # incremental pull it is skipped (incremental never soft-deletes).
            continue
        seen.add(guid)
        fields = _extract_fields(resource, entity)
        row = existing.get(guid)
        if row is None:
            row = model(guid=guid, location_guid=location_guid, **fields)
            row.deleted = False
            row.last_synced = now
            session.add(row)
            existing[guid] = row
            upserted += 1
            continue
        changed = False
        for col, value in fields.items():
            if getattr(row, col) != value:
                setattr(row, col, value)
                changed = True
        if row.deleted:
            row.deleted = False  # revival
            changed = True
        if changed:
            upserted += 1
        row.last_synced = now

    if full:
        # Correctness rule: ONLY a successful full pull may soft-delete.
        for guid, row in existing.items():
            if guid not in seen and not row.deleted:
                row.deleted = True
                row.last_synced = now
                soft_deleted += 1

    return upserted, soft_deleted


def _sync_resource(session, client, location_key: str, restaurant_guid: str,
                   resource: str, *, force_full: bool) -> tuple[str, int, int]:
    """Sync one (location, resource). Returns (mode, upserted, soft_deleted)."""
    state = (
        session.query(FloorSyncState)
        .filter_by(location_guid=restaurant_guid, resource=resource)
        .one_or_none()
    )
    if state is None:
        state = FloorSyncState(location_guid=restaurant_guid, resource=resource)
        session.add(state)

    watermark = state.last_modified
    incremental_ok = bool(watermark) and watermark != UNSUPPORTED and not force_full
    pull_started = _utcnow()
    mode = "incremental" if incremental_ok else "full"
    entities: list | None = None

    if mode == "incremental":
        try:
            entities = client.fetch_config_since(
                _RESOURCE_URL_LEAF[resource], restaurant_guid, watermark
            ) or []
        except ToastError as err:
            if _is_param_rejection(err):
                # Toast rejected lastModified -> full pulls permanently
                # (sentinel persisted in floor_sync_state).
                log.warning(
                    "toast_config_sync: lastModified rejected for %s/%s; "
                    "falling back to full pulls permanently",
                    location_key, resource,
                )
                state.last_modified = UNSUPPORTED
                mode = "full"
            else:
                raise

    if mode == "full":
        entities = _pull_full(client, location_key, restaurant_guid, resource)

    now = _utcnow()
    upserted, soft_deleted = _apply(
        session, _RESOURCE_MODEL[resource], restaurant_guid, entities,
        full=(mode == "full"), resource=resource, now=now,
    )

    if state.last_modified != UNSUPPORTED:
        state.last_modified = _fmt_watermark(
            pull_started - timedelta(minutes=WATERMARK_OVERLAP_MINUTES)
        )
    state.last_run_at = now
    return mode, upserted, soft_deleted


def _default_session():
    from app import db as app_db  # late import: only the real path needs it
    if app_db.SessionLocal is None or app_db.engine is None:
        raise RuntimeError("DATABASE_URL not set; cannot open a sync session")
    ensure_floor_tables(app_db.engine)
    return app_db.SessionLocal()


def sync_location(location_key: str, *, client=None, session=None,
                  force_full: bool = False) -> dict:
    """Sync Toast tables + service areas for one location key
    ('copperfield' | 'tomball'). Returns the contract counts dict.

    All-or-nothing per location: any pull/apply failure rolls back the
    whole location so a partial sync can never soft-delete or half-write.
    `client` / `session` are injectable for tests; defaults are the shared
    ToastClient and an app.db session (floor tables ensured).
    """
    guids = restaurant_guids()
    if location_key not in guids:
        raise ValueError(
            f"unknown Toast location key {location_key!r}; have {sorted(guids)}"
        )
    restaurant_guid = guids[location_key]
    client = client if client is not None else ToastClient.shared()

    own_session = session is None
    if own_session:
        session = _default_session()
    try:
        modes: dict[str, str] = {}
        counts: dict = {}
        for resource in RESOURCES:
            mode, upserted, soft_deleted = _sync_resource(
                session, client, location_key, restaurant_guid, resource,
                force_full=force_full,
            )
            modes[resource] = mode
            counts[f"{resource}_upserted"] = upserted
            counts[f"{resource}_soft_deleted"] = soft_deleted
        counts["source"] = (
            "incremental"
            if all(m == "incremental" for m in modes.values())
            else "full"
        )
        session.commit()
        log.info("toast_config_sync: %s -> %s", location_key, counts)
        return counts
    except Exception:
        session.rollback()
        raise
    finally:
        if own_session:
            session.close()


def sync_all(*, client=None, session=None, force_full: bool = False) -> dict:
    """Sync every location that has a restaurant GUID configured.
    Returns {location_key: counts}."""
    return {
        key: sync_location(key, client=client, session=session,
                           force_full=force_full)
        for key in sorted(restaurant_guids())
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = list(sys.argv[1:] if argv is None else argv)
    force_full = "--full" in args
    args = [a for a in args if a != "--full"]
    target = args[0] if args else "all"
    if target == "all":
        result = sync_all(force_full=force_full)
    else:
        result = sync_location(target, force_full=force_full)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
