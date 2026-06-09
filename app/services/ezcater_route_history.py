"""Route history captured from ezCater live tracking.

The live ezCater API only returns the driver's current location. To keep a
route after the tracker disappears, we append each sampled location while the
delivery is live and summarize those samples for payroll and profile pages.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any, Sequence

from app.models import Driver, EzcaterTrackingPoint, Order

TOMBALL_ORIGIN_STORES = {"store_2", "store_4"}
COPPERFIELD_ORIGIN_STORES = {"store_1", "store_3"}
MILES_THRESHOLD = 20.0


@dataclass
class RouteSummary:
    order_id: int
    point_count: int = 0
    distance_miles: float = 0.0
    extra_miles_over_20: float = 0.0
    duration_minutes: int = 0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    driver_id: int | None = None
    driver_name: str | None = None
    status_key: str | None = None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_lat_lng(lat: float | None, lng: float | None) -> bool:
    return lat is not None and lng is not None and -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0


def _first_driver(body: dict[str, Any] | None) -> dict[str, Any] | None:
    data = (body or {}).get("data") or {}
    drivers = data.get("drivers") or []
    if isinstance(drivers, list) and drivers:
        first = drivers[0]
        return first if isinstance(first, dict) else None
    return None


def _driver_name(driver_payload: dict[str, Any] | None) -> str | None:
    if not driver_payload:
        return None
    for key in ("name", "driverName", "fullName"):
        value = driver_payload.get(key)
        if value:
            return str(value).strip() or None
    return None


def _status_key(body: dict[str, Any] | None, driver_payload: dict[str, Any] | None) -> str | None:
    current = (driver_payload or {}).get("currentStatus") or {}
    if isinstance(current, dict) and current.get("key"):
        return str(current["key"])
    data = (body or {}).get("data") or {}
    status = data.get("status")
    return str(status) if status else None


def _origin_location(order: Order) -> str | None:
    origin = (order.origin_store_id or order.reported_store_id or "").strip().lower()
    pickup = (order.pickup_kitchen or "").strip().lower()
    if origin in TOMBALL_ORIGIN_STORES or pickup == "tomball":
        return "tomball"
    if origin in COPPERFIELD_ORIGIN_STORES or pickup == "copperfield":
        return "copperfield"
    return None


def _resolve_driver(db, order: Order, name: str | None) -> Driver | None:
    if order.assigned_driver_id:
        return db.get(Driver, order.assigned_driver_id)
    target_name = name or order.ezcater_driver_name
    if not target_name:
        return None
    from app.services.ezcater_payroll import normalize_driver_name

    norm_target = normalize_driver_name(target_name)
    if not norm_target:
        return None
    q = db.query(Driver).filter(Driver.name.isnot(None))
    location = _origin_location(order)
    if location:
        q = q.filter(Driver.location == location)
    for driver in q.all():
        if normalize_driver_name(driver.name) == norm_target:
            return driver
    return None


def _same_sample(a: EzcaterTrackingPoint, lat: float, lng: float, status_key: str | None) -> bool:
    return (
        round(a.lat, 6) == round(lat, 6)
        and round(a.lng, 6) == round(lng, 6)
        and (a.provider_status_key or "") == (status_key or "")
    )


def record_tracking_sample(
    db,
    order: Order,
    body: dict[str, Any] | None,
    *,
    captured_at: datetime | None = None,
    dedupe_seconds: int = 30,
) -> EzcaterTrackingPoint | None:
    """Append one ezCater location sample for an order if the response has GPS.

    Returns the new point, or None when there is no valid location or the same
    sample was already recorded moments ago.
    """
    if not order.delivery_tracking_id:
        return None
    captured_at = captured_at or datetime.utcnow()
    driver_payload = _first_driver(body)
    status_key = _status_key(body, driver_payload)
    name = _driver_name(driver_payload) or order.ezcater_driver_name

    if driver_payload or (status_key or "").startswith("driver_"):
        order.tracking_status = "Tracked"
    if name and not order.ezcater_driver_name:
        order.ezcater_driver_name = name

    matched_driver = _resolve_driver(db, order, name)
    if matched_driver and not order.assigned_driver_id:
        order.assigned_driver_id = matched_driver.id
        if not order.assigned_driver:
            order.assigned_driver = matched_driver.name

    loc = (driver_payload or {}).get("currentLocation") or {}
    lat = _safe_float(loc.get("latitude"))
    lng = _safe_float(loc.get("longitude"))
    if not _valid_lat_lng(lat, lng):
        return None

    order.ezcater_driver_lat = lat
    order.ezcater_driver_lng = lng
    if status_key:
        order.ezcater_status_key = status_key
    order.ezcater_status_updated_at = captured_at

    last = (
        db.query(EzcaterTrackingPoint)
        .filter(EzcaterTrackingPoint.order_id == order.id)
        .order_by(EzcaterTrackingPoint.captured_at.desc(), EzcaterTrackingPoint.id.desc())
        .first()
    )
    if last and _same_sample(last, lat, lng, status_key):
        age = (captured_at - last.captured_at).total_seconds()
        if age < dedupe_seconds:
            return None

    point = EzcaterTrackingPoint(
        order_id=order.id,
        driver_id=(matched_driver.id if matched_driver else order.assigned_driver_id),
        tracking_uuid=order.delivery_tracking_id,
        driver_name=name,
        provider_status_key=status_key,
        captured_at=captured_at,
        lat=lat,
        lng=lng,
    )
    db.add(point)
    return point


def _haversine_miles(a: EzcaterTrackingPoint, b: EzcaterTrackingPoint) -> float:
    radius_miles = 3958.7613
    lat1, lng1, lat2, lng2 = map(radians, [a.lat, a.lng, b.lat, b.lng])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 2 * radius_miles * asin(sqrt(h))


def _summarize(order_id: int, points: list[EzcaterTrackingPoint]) -> RouteSummary:
    if not points:
        return RouteSummary(order_id=order_id)
    miles = 0.0
    for prev, cur in zip(points, points[1:]):
        miles += _haversine_miles(prev, cur)
    started_at = points[0].captured_at
    ended_at = points[-1].captured_at
    duration_minutes = max(0, int((ended_at - started_at).total_seconds() // 60))
    last_with_driver = next((p for p in reversed(points) if p.driver_id or p.driver_name), points[-1])
    return RouteSummary(
        order_id=order_id,
        point_count=len(points),
        distance_miles=miles,
        extra_miles_over_20=max(0.0, miles - MILES_THRESHOLD),
        duration_minutes=duration_minutes,
        started_at=started_at,
        ended_at=ended_at,
        driver_id=last_with_driver.driver_id,
        driver_name=last_with_driver.driver_name,
        status_key=points[-1].provider_status_key,
    )


def route_points_for_order(db, order_id: int) -> list[EzcaterTrackingPoint]:
    return (
        db.query(EzcaterTrackingPoint)
        .filter(EzcaterTrackingPoint.order_id == order_id)
        .order_by(EzcaterTrackingPoint.captured_at.asc(), EzcaterTrackingPoint.id.asc())
        .all()
    )


def route_summary_for_order(db, order_id: int) -> RouteSummary:
    return _summarize(order_id, route_points_for_order(db, order_id))


def route_summary_by_order_ids(db, order_ids: Sequence[int]) -> dict[int, RouteSummary]:
    ids = sorted({int(order_id) for order_id in order_ids if order_id})
    if not ids:
        return {}
    rows = (
        db.query(EzcaterTrackingPoint)
        .filter(EzcaterTrackingPoint.order_id.in_(ids))
        .order_by(EzcaterTrackingPoint.order_id.asc(), EzcaterTrackingPoint.captured_at.asc(), EzcaterTrackingPoint.id.asc())
        .all()
    )
    grouped: dict[int, list[EzcaterTrackingPoint]] = {order_id: [] for order_id in ids}
    for row in rows:
        grouped.setdefault(row.order_id, []).append(row)
    return {order_id: _summarize(order_id, points) for order_id, points in grouped.items()}


def route_point_dicts(db, order_id: int) -> list[dict[str, Any]]:
    return [
        {
            "id": p.id,
            "lat": p.lat,
            "lng": p.lng,
            "captured_at": p.captured_at.isoformat() + "Z",
            "driver_id": p.driver_id,
            "driver_name": p.driver_name,
            "status_key": p.provider_status_key,
        }
        for p in route_points_for_order(db, order_id)
    ]
