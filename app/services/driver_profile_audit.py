from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models import DriverEvent, DriverFile, Order


def record_driver_event(
    db: Session,
    event_type: str,
    *,
    driver_id: int | None = None,
    order_id: int | None = None,
    file_id: int | None = None,
    source: str | None = "app",
    actor_type: str | None = None,
    actor_id: str | int | None = None,
    payload: dict[str, Any] | None = None,
) -> DriverEvent:
    event = DriverEvent(
        driver_id=driver_id,
        order_id=order_id,
        file_id=file_id,
        event_type=event_type,
        source=source,
        actor_type=actor_type,
        actor_id=str(actor_id) if actor_id is not None else None,
        payload_json=payload,
    )
    db.add(event)
    return event


def _driver_order_uploads_dir() -> Path:
    return Path(os.environ.get("DRIVER_ORDER_UPLOADS_DIR", "/var/data/driver-order-uploads"))


def _legacy_static_upload_path(stored_url: str | None) -> Path | None:
    if not stored_url or not stored_url.startswith("/static/"):
        return None
    relative = stored_url.split("?", 1)[0][len("/static/"):]
    static_root = Path(__file__).resolve().parents[1] / "static"
    candidate = (static_root / relative).resolve()
    try:
        candidate.relative_to(static_root.resolve())
    except ValueError:
        return None
    return candidate


def _file_hash(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _find_order_file(order_id: int, kind: str, stored_url: str | None) -> tuple[Path | None, str | None]:
    if not stored_url:
        return None, None
    filename = Path(str(stored_url).split("?", 1)[0]).name
    if not filename:
        return None, None
    candidates = [_driver_order_uploads_dir() / str(order_id) / kind / filename]
    legacy = _legacy_static_upload_path(stored_url)
    if legacy is not None and legacy.name == filename:
        candidates.append(legacy)
    found = next((path for path in candidates if path.exists() and path.is_file()), None)
    return found, filename


def upsert_driver_file_for_order(
    db: Session,
    order: Order,
    kind: str,
    stored_url: str | None,
    *,
    source: str = "order_url",
    uploaded_at: datetime | None = None,
) -> DriverFile | None:
    if not stored_url:
        return None
    found, filename = _find_order_file(order.id, kind, stored_url)
    public_route = str(stored_url).split("?", 1)[0]
    existing = (
        db.query(DriverFile)
        .filter(DriverFile.order_id == order.id)
        .filter(DriverFile.kind == kind)
        .filter(DriverFile.public_route == public_route)
        .first()
    )
    row = existing or DriverFile(order_id=order.id, kind=kind, public_route=public_route)
    if existing is None:
        db.add(row)
    row.driver_id = order.assigned_driver_id
    row.filename = filename
    row.source_url = stored_url
    row.storage_path = str(found) if found else None
    row.exists = bool(found)
    row.size_bytes = found.stat().st_size if found else None
    row.sha256 = _file_hash(found) if found else None
    row.last_checked_at = datetime.utcnow()
    row.uploaded_at = uploaded_at
    row.source = source
    row.meta_json = {
        "external_order_id": order.external_order_id,
        "legacy_static": bool(stored_url.startswith("/static/")),
    }
    return row


def backfill_driver_files_from_orders(db: Session) -> dict[str, int]:
    created_or_seen = 0
    available = 0
    orders = (
        db.query(Order)
        .filter((Order.setup_photo_url.isnot(None)) | (Order.parking_photo_url.isnot(None)))
        .all()
    )
    for order in orders:
        for kind, stored_url, uploaded_at in (
            ("delivery", order.setup_photo_url, order.setup_photo_uploaded_at),
            ("parking", order.parking_photo_url, order.parking_photo_uploaded_at),
        ):
            row = upsert_driver_file_for_order(
                db,
                order,
                kind,
                stored_url,
                source="startup_backfill",
                uploaded_at=uploaded_at,
            )
            if row is not None:
                created_or_seen += 1
                if row.exists:
                    available += 1
    return {"file_refs": created_or_seen, "available": available}
