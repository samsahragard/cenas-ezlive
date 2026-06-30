"""Isolated READ-ONLY driver + ezCater-orders data-center EXPORT endpoint
(Sam #3592/#3601/#3604/#3610; aick #3609 contract, frozen by CK #3612).

Serves CK's R1-minimized driver/orders marts (Mini_IT13) the app-DB rows they
cannot read directly -- field-whitelisted to the FROZEN contract. The endpoint
serves only the per-ROW data; CK DERIVES all rollups/aggregates on 13 (dm_pay
periods, dm_customer aggregates + k-anon, dm_attendance rates, meta, ledger).

R1 DATA-MINIMIZATION (aick #3598 / Sam #3601), enforced HERE by construction:
  * Raw GPS lat/lng are read TRANSIENTLY (local vars) to compute a DERIVED
    gps_miles summary, and are NEVER placed in any returned dict.
  * Customer cleartext (client + phone) is read TRANSIENTLY to compute a keyed
    HMAC customer_hash app-side; the cleartext is NEVER returned. 13 receives
    only the opaque hash + salt_version -> can MATCH but, lacking salt+cleartext,
    can NEVER reverse (aick #3603). Salt lives ONLY in app env (DRIVERDC_HMAC_SALT).
  * Delivery address / instructions / gate-code / customer contact, driver
    PII/auth, parking-receipt image, and the HMAC salt are NEVER served.

ISOLATION (Sam #3178): imports ONLY stdlib + flask + app.db + app.models.
NEVER driver_system (frozen catering/driver coupling). No ezcater_payroll import
either -- per-delivery pay is served from STORED Order columns; CK derives the
$ breakdown. Same isolation discipline as datamart_export_routes / perf_push_routes.

AUTH (aick #3182 fail-closed): a dedicated DRIVERDC_EXPORT_TOKEN (separate from
CRON_TOKEN and DATAMART_EXPORT_TOKEN). Unset/empty/wrong -> 403 (never fail-open).
Token-gated /cron path; NOT driver- or employee-facing.

STATUS: PREPARE-ONLY. Blueprint is authored but NOT registered on prod, no token
persisted, no scheduler -- nothing enabled until explicit Sam GO (Sam #3601/#3610).
"""
import os
import hmac
import hashlib
import math
import json as _json

import base64
from datetime import datetime
from pathlib import Path

from flask import Blueprint, abort, jsonify, request

from app.db import SessionLocal
from app.models import (
    Cancellation,
    DeliveryRequest,
    Driver,
    DriverApplication,
    DriverLocation,
    DriverLog,
    DriverNotification,
    DriverScore,
    DriverShift,
    EzcaterOrderDetails,
    EzcaterTrackingPoint,
    ManagerMessage,
    Order,
    OrderItem,
    PayCheck,
)

driverdc_export_bp = Blueprint("driverdc_export", __name__)


def _extract_token():
    """Self-contained token read (no driver_system import). Precedence:
    Authorization: Bearer <t> -> X-Driverdc-Token header -> ?token= query."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("X-Driverdc-Token") or request.args.get("token")


def _norm_customer(client, phone):
    """Normalize name+phone for a stable hash. Cleartext used TRANSIENTLY only."""
    name = " ".join((client or "").strip().lower().split())
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    if not name and not digits:
        return None
    return name + "|" + digits


def _customer_hash(client, phone, salt, salt_version):
    """Keyed HMAC pseudonym, computed APP-SIDE. Returns (hash_hex, version) or
    (None, None). The salt + cleartext NEVER leave this process; only the hash
    + version are returned (aick #3603). No salt -> no hash (fail-safe; the
    customer dimension is simply absent until the salt is provisioned)."""
    if not salt:
        return None, None
    norm = _norm_customer(client, phone)
    if norm is None:
        return None, None
    h = hmac.new(salt.encode("utf-8"), norm.encode("utf-8"), hashlib.sha256).hexdigest()
    return h, salt_version


def _haversine_mi(a_lat, a_lng, b_lat, b_lng):
    R = 3958.7613  # earth radius, miles
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dphi = math.radians(b_lat - a_lat)
    dl = math.radians(b_lng - a_lng)
    x = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(x)))


def _gps_summary(db, driver_id, order_id):
    """DERIVED our-GPS summary for one delivery. Raw lat/lng are read into LOCAL
    vars only to sum trip miles; they are NEVER returned (R1a). Returns
    (gps_miles_or_None, tracking_ok_bool)."""
    if not driver_id or not order_id:
        return None, False
    fixes = (
        db.query(DriverLocation.lat, DriverLocation.lng)
          .filter(DriverLocation.driver_id == driver_id,
                  DriverLocation.order_id == order_id)
          .order_by(DriverLocation.captured_at.asc())
          .all()
    )
    if not fixes:
        return None, False
    miles = 0.0
    prev = None
    for lat, lng in fixes:          # lat/lng: TRANSIENT locals, never serialized
        if lat is None or lng is None:
            continue
        if prev is not None:
            miles += _haversine_mi(prev[0], prev[1], lat, lng)
        prev = (lat, lng)
    return round(miles, 2), True


def _iso(dt):
    return dt.isoformat() if dt else None


def _row_dict(row) -> dict:
    out = {}
    for col in row.__table__.columns:
        value = getattr(row, col.name)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        out[col.name] = value
    return out


def _all_rows(db, model) -> list[dict]:
    return [_row_dict(row) for row in db.query(model).all()]


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


def _upload_payload(order: Order, kind: str, stored_url: str | None) -> dict | None:
    if not stored_url:
        return None
    filename = Path(str(stored_url).split("?", 1)[0]).name
    if not filename:
        return None
    candidates = [
        _driver_order_uploads_dir() / str(order.id) / kind / filename,
    ]
    legacy = _legacy_static_upload_path(stored_url)
    if legacy is not None and legacy.name == filename:
        candidates.append(legacy)
    found = next((path for path in candidates if path.exists() and path.is_file()), None)
    payload = {
        "order_id": order.id,
        "external_order_id": order.external_order_id,
        "driver_id": order.assigned_driver_id,
        "kind": kind,
        "filename": filename,
        "stored_url": stored_url,
        "available": bool(found),
        "size_bytes": found.stat().st_size if found else None,
        "file_b64": None,
    }
    if found and found.stat().st_size <= int(os.getenv("DRIVER_LOCAL_EXPORT_MAX_FILE_BYTES", "8000000")):
        payload["file_b64"] = base64.b64encode(found.read_bytes()).decode("ascii")
    return payload


@driverdc_export_bp.route("/cron/driver-local-export", methods=["GET"])
def driver_local_export():
    """Owner-only full driver mirror export.

    Unlike /cron/driverdc-export, this is intentionally NOT R1-minimized. Sam
    asked for an internal local DB for everything drivers do: driver records,
    applications, shifts, GPS, requests, notifications, paychecks, messages,
    cancellations, assigned orders, order items, tracking points, and uploaded
    proof/receipt files when the bytes still exist on server storage.
    """
    expected = os.getenv("DRIVERDC_EXPORT_TOKEN")
    if not expected or _extract_token() != expected:
        abort(403)

    db = SessionLocal()
    try:
        orders = db.query(Order).all()
        upload_files = []
        for order in orders:
            for payload in (
                _upload_payload(order, "delivery", order.setup_photo_url),
                _upload_payload(order, "parking", order.parking_photo_url),
            ):
                if payload:
                    upload_files.append(payload)

        return jsonify({
            "ok": True,
            "contract": "driver-local-v1-complete",
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "tables": {
                "drivers": _all_rows(db, Driver),
                "driver_application": _all_rows(db, DriverApplication),
                "driver_logs": _all_rows(db, DriverLog),
                "driver_shift": _all_rows(db, DriverShift),
                "driver_location": _all_rows(db, DriverLocation),
                "delivery_request": _all_rows(db, DeliveryRequest),
                "driver_notification": _all_rows(db, DriverNotification),
                "driver_score": _all_rows(db, DriverScore),
                "paycheck": _all_rows(db, PayCheck),
                "cancellation": _all_rows(db, Cancellation),
                "manager_message": _all_rows(db, ManagerMessage),
                "orders": [_row_dict(order) for order in orders],
                "order_items": _all_rows(db, OrderItem),
                "ezcater_tracking_point": _all_rows(db, EzcaterTrackingPoint),
            },
            "upload_files": upload_files,
            "counts": {
                "upload_file_refs": len(upload_files),
                "upload_files_available": sum(1 for f in upload_files if f["available"]),
            },
        })
    finally:
        db.close()


@driverdc_export_bp.route("/cron/driverdc-export", methods=["GET"])
def driverdc_export():
    # FAIL-CLOSED (aick #3182): unset/empty token MUST 403. Dedicated token.
    expected = os.getenv("DRIVERDC_EXPORT_TOKEN")
    if not expected or _extract_token() != expected:
        abort(403)

    # App-side HMAC salt (NEVER leaves this process). Unset -> hashes are None
    # (fail-safe); the export still serves everything else.
    salt = os.getenv("DRIVERDC_HMAC_SALT")
    salt_version = os.getenv("DRIVERDC_HMAC_SALT_VERSION", "v1")

    db = SessionLocal()
    try:
        # ---- drivers (dm_driver: identity/status/store/tier/score) ----
        drivers = [
            {
                "driver_id": d.id,
                "name": d.name,                                # [DRV own / CORP]
                "active": bool(d.active),
                "status": d.status,
                "home_store_key": d.home_store_id,
                "current_tier": d.current_tier,
                "current_score": d.current_score,
                "joined_at": _iso(d.joined_at),
                "lifetime_delivery_count": d.lifetime_delivery_count,
            }
            for d in db.query(
                Driver.id, Driver.name, Driver.active, Driver.status,
                Driver.home_store_id, Driver.current_tier, Driver.current_score,
                Driver.joined_at, Driver.lifetime_delivery_count,
            ).all()
        ]
        # NB: email/phone/address/password_hash/passcode_hash/auth-state/
        # last_known_lat/lng are NOT in the select -> never loaded, never served.

        # ---- driver_scores (dm_driver_score: history snapshots) ----
        driver_scores = [
            {
                "driver_id": r.driver_id, "computed_at": _iso(r.computed_at),
                "window_start": _iso(r.window_start), "window_end": _iso(r.window_end),
                "score": r.score, "tier": r.tier,
                "tracking_pts": r.tracking_pts, "on_time_pts": r.on_time_pts,
                "cancellation_pts": r.cancellation_pts, "photo_pts": r.photo_pts,
                "response_pts": r.response_pts, "star_pts": r.star_pts,
            }
            for r in db.query(
                DriverScore.driver_id, DriverScore.computed_at,
                DriverScore.window_start, DriverScore.window_end,
                DriverScore.score, DriverScore.tier, DriverScore.tracking_pts,
                DriverScore.on_time_pts, DriverScore.cancellation_pts,
                DriverScore.photo_pts, DriverScore.response_pts, DriverScore.star_pts,
            ).all()
        ]

        # ---- orders + per-delivery + items + driver-link + timing ----
        # Select ONLY whitelisted columns (never the whole entity) so cleartext
        # customer / raw GPS / instructions are not even loaded into the orders rows.
        order_cols = db.query(
            Order.id, Order.external_order_id, Order.external_delivery_id,
            Order.origin_store_id, Order.delivery_date,
            Order.delivery_window_start, Order.delivery_window_end,
            Order.status, Order.headcount, Order.tracking_status,
            Order.total_amount, Order.food_total, Order.delivery_fee,
            Order.tip_amount, Order.caterer_total_due,
            Order.assigned_driver_id, Order.ezcater_driver_name,
            Order.delivery_result, Order.delivery_start_time,
            Order.delivery_complete_time, Order.delivered_actual_at,
            Order.setup_photo_url, Order.parking_photo_url, Order.parking_cost,
            Order.paid_payout, Order.potential_payout,
            Order.pay_verified_miles, Order.pay_driven_miles, Order.pickup_miles,
            Order.pay_bonus_tracked, Order.pay_five_star,
            # client + customer_phone: TRANSIENT for the hash ONLY (see below).
            Order.client, Order.customer_phone,
        ).all()

        # Granular ezCater fees (Sam #3659): select ONLY the 3 fee cols (cents) from
        # ezcater_order_details. That table ALSO holds PII (gate_code / day_of_contact /
        # special_instructions / items_json) -- DELIBERATELY NOT selected, so PII is never
        # loaded or served. discounts/taxes/toast_app_total have NO source column (stay NULL).
        fee_by_order = {
            ext_id: (comm_c, svc_c, proc_c)
            for ext_id, comm_c, svc_c, proc_c in db.query(
                EzcaterOrderDetails.external_order_id,
                EzcaterOrderDetails.commission_cents,
                EzcaterOrderDetails.service_fee_cents,
                EzcaterOrderDetails.processing_fee_cents,
            ).all()
        }

        def _c2usd(c):
            return round(c / 100.0, 2) if c is not None else None

        orders, deliveries, order_drivers, order_timings = [], [], [], []
        for o in order_cols:
            cust_hash, cust_ver = _customer_hash(o.client, o.customer_phone, salt, salt_version)
            payout = o.paid_payout if o.paid_payout is not None else o.potential_payout
            ezt = o.total_amount
            gmp = (round((ezt or 0) - (payout or 0), 2)) if (ezt is not None or payout is not None) else None
            fees = fee_by_order.get(o.external_order_id, (None, None, None))

            orders.append({
                "external_order_id": o.external_order_id,
                "external_delivery_id": o.external_delivery_id,
                "store_key": o.origin_store_id,
                "delivery_date": o.delivery_date,
                "window_start": _iso(o.delivery_window_start),
                "window_end": _iso(o.delivery_window_end),
                "status": o.status,
                "headcount": o.headcount,
                "tracking_status": o.tracking_status,
                # [SRC] economics (only the columns that exist; rest are CK NULLs):
                "ezcater_total": ezt,
                "food_total": o.food_total,
                "delivery_fee": o.delivery_fee,
                "tip_amount": o.tip_amount,
                "caterer_total_due": o.caterer_total_due,
                # granular ezCater fees (Sam #3659; cents->$ from ezcater_order_details, fee cols ONLY):
                "commissions": _c2usd(fees[0]),
                "service_fees": _c2usd(fees[1]),
                "processing_fees": _c2usd(fees[2]),
                # discounts / taxes / toast_app_total: NO source column -> NULL until an ingest follow-up
                "gross_minus_payout": gmp,
                # [INT] opaque pseudonym ONLY -- NO cleartext customer anywhere:
                "customer_hash": cust_hash,
                "customer_salt_version": cust_ver,
            })

            order_drivers.append({
                "external_order_id": o.external_order_id,
                "driver_id": o.assigned_driver_id,
                "link_method": ("fk" if o.assigned_driver_id
                                else ("fuzzy_name" if o.ezcater_driver_name else "unlinked")),
                "ezcater_driver_name": o.ezcater_driver_name,
            })

            on_time = None
            if o.delivered_actual_at and o.delivery_window_end:
                on_time = 1 if o.delivered_actual_at <= o.delivery_window_end else 0
            order_timings.append({
                "external_order_id": o.external_order_id,
                "delivery_result": o.delivery_result,
                "delivery_start": o.delivery_start_time,
                "delivery_complete": o.delivery_complete_time,
                "delivered_actual_at": _iso(o.delivered_actual_at),
                "on_time": on_time,
            })

            if o.assigned_driver_id:
                gps_miles, tracking_ok = _gps_summary(db, o.assigned_driver_id, o.id)
                deliveries.append({
                    "driver_id": o.assigned_driver_id,
                    "external_order_id": o.external_order_id,
                    "business_date": o.delivery_date,
                    "status": o.status,
                    "on_time": on_time,
                    "tracking_ok": 1 if tracking_ok else 0,
                    "proof_photo_present": 1 if o.setup_photo_url else 0,
                    "parking_proof_present": 1 if o.parking_photo_url else 0,
                    "gps_miles": gps_miles,               # DERIVED; raw fixes never served
                    "driver_payout": payout,
                    "parking_cost": o.parking_cost,
                    # pay INPUTS (CK derives the verified-miles/bonus $ breakdown):
                    "pay_verified_miles": o.pay_verified_miles,
                    "pay_driven_miles": o.pay_driven_miles,
                    "pickup_miles": o.pickup_miles,   # [CORP] ezCater route distance (scalar; NO PII / NO GPS coords) -- CK unverified-order auto-estimate input (#3619)
                    "pay_bonus_tracked": bool(o.pay_bonus_tracked) if o.pay_bonus_tracked is not None else None,
                    "pay_five_star": bool(o.pay_five_star) if o.pay_five_star is not None else None,
                })

        # ---- order_items (dm_order_item) ----
        # name from raw_alias (price baked in -> unit_price/line_total NULL, G-ITEM);
        # modifiers from choices/extras JSON (food options, no PII).
        order_items = []
        item_rows = (
            db.query(
                Order.external_order_id, OrderItem.item_key, OrderItem.raw_alias,
                OrderItem.qty, OrderItem.choices, OrderItem.extras,
            )
            .join(Order, OrderItem.order_id == Order.id)
            .all()
        )
        for ext_id, item_key, raw_alias, qty, choices, extras in item_rows:
            mods = None
            if choices or extras:
                mods = _json.dumps({"choices": choices, "extras": extras}, default=str)
            order_items.append({
                "external_order_id": ext_id,
                "item_key": item_key,
                "name": raw_alias,           # [CORP] item string (no PII)
                "category": None,            # G-ITEM: no category column yet
                "menu_group": None,
                "qty": qty,
                "modifiers_json": mods,      # [CORP] food options only
                "unit_price": None,          # [SRC] G-ITEM (baked into raw_alias)
                "line_total": None,
            })

        return jsonify({
            "ok": True,
            "contract": "driverdc-v3-frozen-3612",
            "salt_version": salt_version,
            "counts": {
                "drivers": len(drivers), "driver_scores": len(driver_scores),
                "orders": len(orders), "deliveries": len(deliveries),
                "order_items": len(order_items),
            },
            "drivers": drivers,
            "driver_scores": driver_scores,
            "orders": orders,
            "deliveries": deliveries,
            "order_drivers": order_drivers,
            "order_timings": order_timings,
            "order_items": order_items,
        })
    finally:
        db.close()
