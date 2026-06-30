"""Driver portal: self-service signup + login + log viewing.

Drivers self-register from the main store picker (5th 'Drivers' card). Per
Sam's spec, sign-up is auto-approved — no manager gate. Email is the login
identifier (globally unique). Password is hashed via werkzeug. Account
lockout after 5 failed attempts in 10 min protects against brute force.

Legacy rows in `drivers` (created by the manager dashboard with just name +
location) have a NULL password_hash and can't log in until a manager resets
them via the per-store Drivers admin page (see store_routes.driver_admin).
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import SystemRandom

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from app.db import get_db
from app.models import Driver, DriverLog, DriverShift, DriverLocation, Order
from app.services import delivery_lifecycle as lifecycle

driver = Blueprint("driver", __name__)

LOCATION_LABELS = {
    "copperfield": "Copperfield",
    "tomball": "Tomball",
}
LOCKOUT_THRESHOLD = 5
LOCKOUT_DURATION = timedelta(minutes=10)

# 5-character PIN keypad — same character set as the User keypad (digits + the
# special keys on the pad: * # @ + % - $). Mirrors app/web/keypad_auth.py.
PIN_LEN = 5
PIN_RE = re.compile(rf"^[\d*#@+%\-$]{{{PIN_LEN}}}$")
_rnd = SystemRandom()
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


def _normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def _valid_pin(s: str) -> bool:
    return bool(s and PIN_RE.match(s))


def _generate_temp_pin() -> str:
    """A random 5-digit numeric temp PIN (no special chars for verbal hand-off)."""
    return "".join(str(_rnd.randint(0, 9)) for _ in range(PIN_LEN))


def _format_driver_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    try:
        from zoneinfo import ZoneInfo

        local = value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Chicago"))
        return local.strftime("%b %d %I:%M %p").replace(" 0", " ")
    except Exception:
        return value.strftime("%b %d %I:%M %p")


def _driver_order_uploads_dir() -> Path:
    """Persistent storage for driver delivery proof files.

    Render keeps /var/data across deploys. Local dev falls back to instance/.
    """
    base = os.environ.get("DRIVER_ORDER_UPLOADS_DIR", "/var/data/driver-order-uploads")
    p = Path(base)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        p = Path(current_app.root_path).parent / "instance" / "driver-order-uploads"
        p.mkdir(parents=True, exist_ok=True)
    return p


def _save_driver_order_image(file_storage, driver_id: int, order_id: int, kind: str) -> str | None:
    """Persist one driver order image under persistent storage and return its URL."""
    if not file_storage or not file_storage.filename:
        return None
    safe = secure_filename(file_storage.filename)
    ext = Path(safe).suffix.lower()
    if ext not in _IMAGE_EXTS:
        raise ValueError("Only image uploads are allowed.")
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    filename = f"{kind}-{stamp}{ext}"
    target_dir = _driver_order_uploads_dir() / str(order_id) / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    file_storage.save(target_dir / filename)
    return url_for("driver.driver_order_upload", order_id=order_id, kind=kind, filename=filename)


def _legacy_static_upload_path(stored_url: str | None) -> Path | None:
    if not stored_url or not stored_url.startswith("/static/"):
        return None
    relative = stored_url.split("?", 1)[0][len("/static/"):]
    candidate = (Path(current_app.static_folder) / relative).resolve()
    static_root = Path(current_app.static_folder).resolve()
    try:
        candidate.relative_to(static_root)
    except ValueError:
        return None
    return candidate


@driver.route("/driver/order-uploads/<int:order_id>/<kind>/<path:filename>")
def driver_order_upload(order_id: int, kind: str, filename: str):
    if kind not in {"delivery", "parking"}:
        abort(404)
    safe_filename = Path(filename).name
    if not safe_filename or safe_filename != filename:
        abort(404)

    db = next(get_db())
    try:
        order = db.get(Order, order_id)
        if not order:
            abort(404)
        stored_url = order.setup_photo_url if kind == "delivery" else order.parking_photo_url
        if not stored_url or safe_filename not in stored_url:
            abort(404)

        candidates = [
            _driver_order_uploads_dir() / str(order_id) / kind / safe_filename,
        ]
        legacy = _legacy_static_upload_path(stored_url)
        if legacy is not None and legacy.name == safe_filename:
            candidates.append(legacy)

        for path in candidates:
            if path.exists() and path.is_file():
                return send_file(str(path), max_age=0)
        abort(404)
    finally:
        db.close()


def _parse_parking_cost(raw: str | None) -> float | None:
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    value = float(text)
    if value < 0:
        raise ValueError("Parking cost cannot be negative.")
    return round(value, 2)


def _has_driver_tracking_since(db, driver_id: int, since: datetime | None) -> bool:
    return _has_driver_tracking_for_order_since(db, driver_id, None, since)


def _has_driver_tracking_for_order_since(
    db,
    driver_id: int,
    order_id: int | None,
    since: datetime | None,
) -> bool:
    if order_id is not None:
        order_q = (
            db.query(DriverLocation)
            .filter(DriverLocation.driver_id == driver_id)
            .filter(DriverLocation.order_id == order_id)
        )
        if order_q.count() > 0:
            return True
    q = db.query(DriverLocation).filter(DriverLocation.driver_id == driver_id)
    if since is not None:
        q = q.filter(DriverLocation.captured_at >= since - timedelta(minutes=5))
    return q.count() > 0


def _bind_recent_tracking_to_order(db, driver_id: int, order: Order) -> int:
    """Attach our local GPS fixes to the active order when possible.

    This keeps tracking based on the Cenas driver GPS stream, not ezCater's
    external tracker, while preserving the existing shift playback.
    """
    since = order.en_route_at or order.pickup_actual_at
    q = (
        db.query(DriverLocation)
        .filter(DriverLocation.driver_id == driver_id)
        .filter(DriverLocation.order_id.is_(None))
    )
    if since is not None:
        q = q.filter(DriverLocation.captured_at >= since - timedelta(minutes=5))
    bound = 0
    for loc in q.all():
        loc.order_id = order.id
        bound += 1
    return bound


def _sync_driver_status_log(db, driver_row: Driver, order: Order) -> None:
    order_href = (
        url_for("orders_browse.view_order", external_order_id=order.external_order_id)
        if order.external_order_id else f"/orders/view/{order.id}"
    )
    log = (
        db.query(DriverLog)
        .filter(DriverLog.driver_name == driver_row.name)
        .filter(DriverLog.location == driver_row.location)
        .filter(DriverLog.order_link == order_href)
        .first()
    )
    if log is None:
        log = DriverLog(
            driver_name=driver_row.name,
            pickup_date=order.delivery_date or datetime.utcnow().date().isoformat(),
            order_link=order_href,
            location=driver_row.location,
            logged_by="driver_portal",
        )
        db.add(log)
    log.pickup_date = order.delivery_date or log.pickup_date
    log.ex_miles = round(order.pickup_miles) if order.pickup_miles is not None else None
    verified = order.pay_verified_miles
    if verified is None and order.pickup_miles is not None:
        verified = max(0.0, order.pickup_miles - 20.0)
    log.ex_miles_verified = round(verified) if verified is not None else None
    log.on_time = bool(
        order.delivered_actual_at
        and order.delivery_window_end
        and order.delivered_actual_at <= order.delivery_window_end
    )
    log.tracking = (order.tracking_status or "").strip().lower() == "tracked"
    log.picture = bool(order.setup_photo_url)
    log.five_star = bool(order.pay_five_star or order.customer_rating == 5)
    parking_note = ""
    if order.parking_cost is not None:
        parking_note = f" Parking ${order.parking_cost:.2f}"
        if order.parking_photo_url:
            parking_note += " with receipt."
        else:
            parking_note += " no receipt."
    log.notes = (
        f"Completed {_format_driver_dt(order.delivered_actual_at) if order.delivered_actual_at else 'pending'}."
        f"{parking_note}"
    )


@driver.route("/driver", methods=["GET"])
def driver_root():
    return redirect(url_for("driver.driver_login"))


# Stable in-app link to the latest Android APK. Redirects to the
# "android-debug-latest" GitHub Release published by the
# .github/workflows/mobile-android.yml workflow on each successful
# build. Drivers hit this from the signup page after creating their
# account, sideload the APK, then use the app instead of the browser
# for real background GPS tracking.
@driver.route("/driver/app.apk", methods=["GET"])
def driver_app_apk():
    return redirect(
        "https://github.com/samsahragard/cenas-ezlive/releases/download/"
        "android-debug-latest/cenas-driver.apk",
        code=302,
    )


@driver.route("/driver/login", methods=["GET"])
def driver_login():
    """Sam #1591 (2026-05-15): the login form is now unified at
    /keypad-login (phone + PIN for everyone). This URL stays as a
    permanent redirect so old links, bookmarks, and the post-logout
    ?_clear=1 path keep working.

    Already-signed-in drivers still short-circuit to /driver/logs so the
    redirect chain doesn't bounce them through the unified form. Query
    string is preserved (esp. ?_clear=1 from /driver/logout, which the
    driver_keypad_login.html JS reads to wipe the persisted phone)."""
    if session.get("driver_id"):
        return redirect(url_for("driver_system.my_profile"))
    qs = request.query_string.decode("ascii") if request.query_string else ""
    target = url_for("keypad_auth.login")
    if qs:
        target = f"{target}?{qs}"
    return redirect(target)


@driver.route("/driver/login-legacy", methods=["GET"])
def driver_login_legacy():
    """Pre-unification driver login renderer. Kept callable in case a
    future incident needs the old isolated path — the unified form at
    /keypad-login is the canonical entry going forward. Not linked from
    any nav surface."""
    if session.get("driver_id"):
        return redirect(url_for("driver_system.my_profile"))
    return render_template(
        "driver_keypad_login.html",
        next_url=request.args.get("next") or url_for("driver.driver_logs"),
        passcode_len=PIN_LEN,
        submit_url=url_for("driver.driver_login_submit"),
        signup_url=url_for("driver.driver_signup"),
        prefill_phone=request.args.get("phone", ""),
    )


@driver.route("/driver/login", methods=["POST"])
def driver_login_submit():
    """JSON contract: accept {phone: 'XXX-XXX-XXXX', pin: 'XXXXX'} →
    return {ok: true, next: '/...'} or {ok: false, error: '...'}.

    Phone is the first factor — lookup by normalized digits. PIN check
    is then a single bcrypt on that one driver. Failure mode is
    generic ('phone or PIN doesn't match') so phone enumeration is
    blocked. Per-driver lockout still applies on the PIN side."""
    from app.services.ezcater_known_drivers_seed import normalize_phone
    data = request.get_json(silent=True) or {}
    phone_raw = (data.get("phone") or "").strip()
    pin = (data.get("pin") or data.get("passcode") or "").strip()
    nxt = (data.get("next") or "/driver/logs").strip()
    if not nxt.startswith("/"):
        nxt = "/driver/logs"

    digits = normalize_phone(phone_raw)
    if not digits or not _valid_pin(pin):
        return jsonify({
            "ok": False,
            "error": "Phone or PIN doesn't match.",
        }), 401

    db = next(get_db())
    try:
        # Find the driver whose stored phone normalizes to the same digits.
        # Phones are stored loosely (free-text on signup) so we normalize
        # on the DB side too. Active only.
        found = None
        for d in (db.query(Driver)
                    .filter(Driver.active.is_(True))
                    .filter(Driver.phone.isnot(None))
                    .all()):
            if normalize_phone(d.phone) == digits:
                found = d
                break
        if found is None:
            return jsonify({"ok": False, "error": "Phone or PIN doesn't match."}), 401
        now = datetime.utcnow()
        if found.lockout_until and found.lockout_until > now:
            mins = max(1, int((found.lockout_until - now).total_seconds() // 60) + 1)
            return jsonify({
                "ok": False,
                "error": f"Too many failed attempts. Try again in {mins} min.",
            }), 429
        if not found.passcode_hash or not check_password_hash(found.passcode_hash, pin):
            # Bump per-driver failed_attempts now that we've matched the
            # phone — the lockout window protects against PIN-guessing on
            # that specific account. Generic error so the attacker can't
            # tell whether the phone was wrong or the PIN was wrong.
            found.failed_attempts = (found.failed_attempts or 0) + 1
            if found.failed_attempts >= LOCKOUT_THRESHOLD:
                found.lockout_until = now + LOCKOUT_DURATION
            db.commit()
            return jsonify({"ok": False, "error": "Phone or PIN doesn't match."}), 401
        # Success path: reset counters, set session.
        found.failed_attempts = 0
        found.lockout_until = None
        found.last_login_at = datetime.utcnow() if hasattr(found, "last_login_at") else None
        db.commit()
        # Clear any leftover User-keypad keys (mirrors dd1d1c7 fix).
        for _k in ("user_id", "user_session_version", "partner_auth_ok"):
            session.pop(_k, None)
        session.permanent = True
        session["driver_id"] = found.id
        session["driver_name"] = found.name
        session["driver_location"] = found.location
        session["driver_session_version"] = found.session_version
        if not found.first_login_done:
            return jsonify({"ok": True, "next": url_for("driver.driver_change_passcode")})
        return jsonify({"ok": True, "next": nxt})
    finally:
        db.close()


@driver.route("/driver/signup", methods=["GET"])
def driver_signup():
    # Pre-fill from query string so a user redirected from /request-access
    # (when they pick the "Driver" role) doesn't have to retype their info.
    # `location` is also accepted so the "Add new driver" button on
    # /<store>/drivers can route the admin into the form with their store
    # pre-selected.
    form = {
        "name":  (request.args.get("name") or "").strip(),
        "email": _normalize_email(request.args.get("email")),
        "phone": (request.args.get("phone") or "").strip(),
        "location": (request.args.get("location") or "").strip().lower(),
    }
    # Defensive: only accept the two real store slugs in the location
    # pre-fill. Anything else (typo, garbage, "both") falls back to the
    # blank dropdown so the user picks deliberately.
    if form["location"] not in {"copperfield", "tomball"}:
        form["location"] = ""
    # `prefilled` is true when ANY of the three identity fields arrived via
    # query string — i.e. the user landed here from /request-access (the
    # only caller that passes those args). Template uses it to swap the
    # generic sub-paragraph for a "Step 2 of 2" banner so the redirect feels
    # intentional rather than a silent landing on a different form.
    # Note: location-only pre-fill (admin "Add new driver" button) does NOT
    # set prefilled — that path is admin-initiated, not the request-access
    # Step-2-of-2 flow.
    prefilled = bool(form["name"] or form["email"] or form["phone"])
    return render_template("driver_signup.html", error=None, form=form,
                           prefilled=prefilled)


@driver.route("/driver/signup", methods=["POST"])
def driver_signup_submit():
    form = {
        "name": (request.form.get("name") or "").strip(),
        "email": _normalize_email(request.form.get("email")),
        "phone": (request.form.get("phone") or "").strip(),
        "address": (request.form.get("address") or "").strip(),
        "location": (request.form.get("location") or "").strip().lower(),
    }
    pin = (request.form.get("pin") or "").strip()
    confirm = (request.form.get("pin_confirm") or "").strip()
    err = None
    if not form["name"] or not form["email"] or not pin or not form["location"]:
        err = "Name, email, location, and PIN are required."
    elif form["location"] not in LOCATION_LABELS:
        err = "Pick a valid location (Copperfield or Tomball)."
    elif "@" not in form["email"] or "." not in form["email"].split("@")[-1]:
        err = "Enter a valid email."
    elif not _valid_pin(pin):
        err = "PIN must be exactly 5 characters (digits or * # @ + % - $)."
    elif pin != confirm:
        err = "PINs don't match."
    if err:
        return render_template("driver_signup.html", error=err, form=form), 400

    db = next(get_db())
    try:
        # Email must be globally unique across the system
        if db.query(Driver).filter(Driver.email == form["email"]).first():
            return render_template("driver_signup.html",
                                   error="That email is already registered. Use login instead.",
                                   form=form), 409
        # PIN uniqueness across drivers was dropped 2026-05-13 when login
        # moved to phone-as-first-factor + per-driver bcrypt — the phone
        # discriminates so two drivers with the same PIN are fine.
        new_driver = Driver(
            name=form["name"],
            location=form["location"],
            email=form["email"],
            phone=form["phone"] or None,
            address=form["address"] or None,
            passcode_hash=generate_password_hash(pin),
            first_login_done=True,
            active=True,
        )
        db.add(new_driver)
        try:
            db.commit()
        except IntegrityError:
            # (name, location) collision with a legacy admin-added row
            db.rollback()
            return render_template("driver_signup.html",
                                   error=f"A driver named '{form['name']}' already exists at "
                                         f"{LOCATION_LABELS[form['location']]}. "
                                         "Contact your manager — they may already have started "
                                         "your account.",
                                   form=form), 409
        # Same role-conflict guard as driver_login_submit.
        for _k in ("user_id", "user_session_version", "partner_auth_ok"):
            session.pop(_k, None)
        session["driver_id"] = new_driver.id
        session["driver_name"] = new_driver.name
        session["driver_location"] = new_driver.location
        session["driver_session_version"] = new_driver.session_version
        session.permanent = True
        return redirect(url_for("driver_system.my_profile"))
    finally:
        db.close()


@driver.route("/driver/change-passcode", methods=["GET"])
def driver_change_passcode():
    """Forced after admin reset or for legacy accounts logging in for the first
    time on the PIN flow. Mirrors keypad_auth.change_passcode for User accounts."""
    driver_id = session.get("driver_id")
    if not driver_id:
        return redirect(url_for("driver.driver_login"))
    db = next(get_db())
    try:
        found = db.get(Driver, driver_id)
        if not found:
            session.pop("driver_id", None)
            return redirect(url_for("driver.driver_login"))
        return render_template(
            "driver_change_passcode.html",
            driver=found,
            forced=not found.first_login_done,
            pin_len=PIN_LEN,
            error=None,
        )
    finally:
        db.close()


@driver.route("/driver/change-passcode", methods=["POST"])
def driver_change_passcode_submit():
    driver_id = session.get("driver_id")
    if not driver_id:
        return redirect(url_for("driver.driver_login"))
    new = (request.form.get("new") or "").strip()
    confirm = (request.form.get("confirm") or "").strip()
    db = next(get_db())
    try:
        found = db.get(Driver, driver_id)
        if not found:
            session.pop("driver_id", None)
            return redirect(url_for("driver.driver_login"))
        if not _valid_pin(new):
            return render_template("driver_change_passcode.html",
                                   error="New PIN must be exactly 5 characters (digits or * # @ + % - $).",
                                   driver=found, forced=not found.first_login_done, pin_len=PIN_LEN), 400
        if new != confirm:
            return render_template("driver_change_passcode.html",
                                   error="PINs don't match.",
                                   driver=found, forced=not found.first_login_done, pin_len=PIN_LEN), 400
        if found.passcode_hash and check_password_hash(found.passcode_hash, new):
            return render_template("driver_change_passcode.html",
                                   error="New PIN must be different from your current one.",
                                   driver=found, forced=not found.first_login_done, pin_len=PIN_LEN), 400
        # PIN uniqueness check dropped 2026-05-13 — phone + per-driver
        # bcrypt now discriminates, so PIN collisions between drivers
        # are no longer a security concern.
        found.passcode_hash = generate_password_hash(new)
        found.first_login_done = True
        found.failed_attempts = 0
        found.lockout_until = None
        # Clear the legacy field now that this driver is fully on the PIN flow.
        found.password_hash = None
        db.commit()
        session["driver_session_version"] = found.session_version
        return redirect(url_for("driver_system.my_profile"))
    finally:
        db.close()


@driver.route("/driver/logs", methods=["GET"])
def driver_logs():
    driver_id = session.get("driver_id")
    if not driver_id:
        return redirect(url_for("driver.driver_login"))
    db = next(get_db())
    try:
        found = db.get(Driver, driver_id)
        if not found or not found.active:
            session.pop("driver_id", None)
            session.pop("driver_name", None)
            session.pop("driver_location", None)
            return redirect(url_for("driver.driver_login"))
        logs = (db.query(DriverLog)
                .filter_by(driver_name=found.name, location=found.location)
                .order_by(DriverLog.pickup_date.desc())
                .all())
        open_shift = (db.query(DriverShift)
                      .filter(DriverShift.driver_id == driver_id,
                              DriverShift.ended_at.is_(None))
                      .order_by(DriverShift.started_at.desc())
                      .first())
        return render_template("driver_viewing.html", view="logs", driver=found,
                               location_label=LOCATION_LABELS.get(found.location, found.location),
                               logs=logs, error=None,
                               on_shift=bool(open_shift),
                               shift_started_at=open_shift.started_at.isoformat() if open_shift else None)
    finally:
        db.close()


# Store-scoping for the driver order view (Sam 2026-06-07): a driver only sees
# orders from THEIR store. origin_store_id keys per store mirror the manager
# dashboard (ezcater_routes: store_2/store_4 = Tomball, store_1/store_3 =
# Copperfield). The driver's store is home_store_id ('dos' = Tomball, 'uno' =
# Copperfield, per the keypad STORE_TO_LOCATION), falling back to the legacy
# .location string. Unknown/both -> None: no scoping, so a mis-configured driver
# still sees their assigned work rather than an empty list -- it never mis-scopes,
# it either scopes to the right store or leaves the view as-is.
_TOMBALL_ORIGIN_STORES = ("store_2", "store_4")
_COPPERFIELD_ORIGIN_STORES = ("store_1", "store_3")


def _driver_origin_stores(driver):
    hs = (getattr(driver, "home_store_id", None) or "").strip().lower()
    if hs == "dos":
        return _TOMBALL_ORIGIN_STORES
    if hs == "uno":
        return _COPPERFIELD_ORIGIN_STORES
    loc = (getattr(driver, "location", None) or "").strip().lower()
    if "tomball" in loc or loc == "dos":
        return _TOMBALL_ORIGIN_STORES
    if "copperfield" in loc or loc == "uno":
        return _COPPERFIELD_ORIGIN_STORES
    return None


@driver.route("/driver/orders", methods=["GET"])
def driver_orders():
    driver_id = session.get("driver_id")
    if not driver_id:
        return redirect(url_for("driver.driver_login"))
    db = next(get_db())
    try:
        found = db.get(Driver, driver_id)
        if not found or not found.active:
            session.pop("driver_id", None)
            session.pop("driver_name", None)
            session.pop("driver_location", None)
            return redirect(url_for("driver.driver_login"))
        oq = (
            db.query(Order)
            .filter(Order.assigned_driver_id == found.id)
            .filter(Order.status.in_(["approved", "picked_up", "en_route", "delivered"]))
        )
        _stores = _driver_origin_stores(found)
        if _stores:
            # Sam 2026-06-07: scope the driver's list to their own store only.
            oq = oq.filter(Order.origin_store_id.in_(_stores))
        orders = oq.order_by(
            Order.delivery_date.asc(), Order.deliver_at.asc(), Order.id.asc()
        ).all()
        return render_template(
            "driver_orders.html",
            active="driver_orders",
            driver=found,
            location_label=LOCATION_LABELS.get(found.location, found.location),
            orders=orders,
            fmt_driver_dt=_format_driver_dt,
        )
    finally:
        db.close()


@driver.route("/driver/orders/<int:order_id>/start", methods=["POST"])
def driver_order_start(order_id: int):
    driver_id = session.get("driver_id")
    if not driver_id:
        return redirect(url_for("driver.driver_login"))
    db = next(get_db())
    try:
        order = db.get(Order, order_id)
        if not order or order.assigned_driver_id != driver_id:
            abort(404)
        found = db.get(Driver, driver_id)
        if not found:
            abort(404)
        try:
            order.assigned_driver = found.name
            order.ezcater_driver_name = found.name
            if order.status == "approved":
                lifecycle.mark_picked_up(db, order)
                lifecycle.mark_en_route(db, order)
            elif order.status == "picked_up":
                lifecycle.mark_en_route(db, order)
            elif order.status in {"en_route", "delivered"}:
                pass
            else:
                raise lifecycle.IllegalTransition(f"can't start from status={order.status!r}")
            session["driver_active_order_id"] = order.id
            _bind_recent_tracking_to_order(db, driver_id, order)
            db.commit()
            flash("Delivery started and timestamped.", "ok")
        except lifecycle.IllegalTransition as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("driver.driver_orders"))
    finally:
        db.close()


@driver.route("/driver/orders/<int:order_id>/complete", methods=["POST"])
def driver_order_complete(order_id: int):
    driver_id = session.get("driver_id")
    if not driver_id:
        return redirect(url_for("driver.driver_login"))
    db = next(get_db())
    try:
        order = db.get(Order, order_id)
        if not order or order.assigned_driver_id != driver_id:
            abort(404)
        found = db.get(Driver, driver_id)
        if not found:
            abort(404)

        try:
            now = datetime.utcnow()
            delivery_photo_url = _save_driver_order_image(
                request.files.get("delivery_photo"),
                driver_id,
                order.id,
                "delivery",
            )
            parking_photo_url = _save_driver_order_image(
                request.files.get("parking_photo"),
                driver_id,
                order.id,
                "parking",
            )
            parking_cost = _parse_parking_cost(request.form.get("parking_cost"))

            if delivery_photo_url:
                order.setup_photo_url = delivery_photo_url
                order.setup_photo_uploaded_at = now
            if parking_photo_url:
                order.parking_photo_url = parking_photo_url
                order.parking_photo_uploaded_at = now
            if parking_cost is not None:
                order.parking_cost = parking_cost
            order.assigned_driver = found.name
            order.ezcater_driver_name = found.name

            if order.status == "approved":
                lifecycle.mark_picked_up(db, order)
                lifecycle.mark_en_route(db, order)
                lifecycle.mark_delivered(db, order, setup_photo_url=delivery_photo_url)
            elif order.status == "picked_up":
                lifecycle.mark_en_route(db, order)
                lifecycle.mark_delivered(db, order, setup_photo_url=delivery_photo_url)
            elif order.status == "en_route":
                lifecycle.mark_delivered(db, order, setup_photo_url=delivery_photo_url)
            elif order.status == "delivered":
                # Already complete; allow proof/cost corrections without bumping
                # lifetime delivery count a second time.
                pass
            else:
                raise lifecycle.IllegalTransition(f"can't complete from status={order.status!r}")

            _bind_recent_tracking_to_order(db, driver_id, order)
            if _has_driver_tracking_for_order_since(db, driver_id, order.id, order.en_route_at or order.pickup_actual_at):
                order.tracking_status = "Tracked"
            _sync_driver_status_log(db, found, order)
            session.pop("driver_active_order_id", None)
            db.commit()
            flash("Delivery completion saved and timestamped.", "ok")
        except (ValueError, lifecycle.IllegalTransition) as exc:
            db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("driver.driver_orders"))
    finally:
        db.close()


@driver.route("/driver/logout", methods=["GET", "POST"])
def driver_logout():
    # Accept GET as well as POST — sidebar logout links are plain anchors,
    # and a POST-only logout would 405 white-screen on click. Same shape
    # as /keypad-logout. The keypad_auth after_request hook adds
    # Cache-Control: no-store to this response so the Capacitor WebView
    # can't serve a cached dashboard on app restart.
    #
    # Clear EVERY role key (driver + user + partner gate) so app reopen
    # never lands on a stale dashboard for whichever role is left over.
    # Sam (2026-05-13) hit this after switching between driver-login and
    # partner-keypad-login: clicking Log out only cleared the driver_*
    # keys, so user_id lingered and the partner sidebar kept rendering.
    # Clear Tier-1 auth_ok too; otherwise a bare / app reopen can pass the
    # global gate with no profile and land on the legacy Partner password
    # screen instead of the phone login.
    session.clear()
    # Add ?_clear=1 so the driver_keypad_login JS wipes the persisted
    # phone in localStorage. Logout means "this person is done on this
    # device" — force re-entry on next login. Banking pattern (Sam
    # 2026-05-13). Sam #1591 (2026-05-15): now lands on the unified
    # /keypad-login (NOT the old /driver/login) — that was the symptom
    # report ("when you log out and try to log back in, it automatically
    # goes to the password screen for the Partners"). The ?_clear=1
    # param survives the unified form's URL the same way.
    return redirect(url_for("keypad_auth.login", _clear=1))


@driver.route("/driver/shift/start", methods=["POST"])
def driver_shift_start():
    """Open a new shift. Closes any prior open shift first (defensive — a
    crashed browser tab can leave a shift dangling)."""
    driver_id = session.get("driver_id")
    if not driver_id:
        return jsonify({"error": "not signed in"}), 401
    db = next(get_db())
    try:
        # Close any prior open shifts for this driver
        open_shifts = (db.query(DriverShift)
                       .filter(DriverShift.driver_id == driver_id,
                               DriverShift.ended_at.is_(None))
                       .all())
        now = datetime.utcnow()
        for s in open_shifts:
            s.ended_at = now
        new_shift = DriverShift(driver_id=driver_id, started_at=now)
        db.add(new_shift)
        db.commit()
        db.refresh(new_shift)
        return jsonify({"shift_id": new_shift.id, "started_at": new_shift.started_at.isoformat()})
    finally:
        db.close()


@driver.route("/driver/shift/end", methods=["POST"])
def driver_shift_end():
    driver_id = session.get("driver_id")
    if not driver_id:
        return jsonify({"error": "not signed in"}), 401
    db = next(get_db())
    try:
        open_shift = (db.query(DriverShift)
                      .filter(DriverShift.driver_id == driver_id,
                              DriverShift.ended_at.is_(None))
                      .order_by(DriverShift.started_at.desc())
                      .first())
        if open_shift:
            open_shift.ended_at = datetime.utcnow()
            session.pop("driver_active_order_id", None)
            db.commit()
            return jsonify({"ended_shift_id": open_shift.id})
        return jsonify({"ended_shift_id": None, "note": "no open shift"})
    finally:
        db.close()


@driver.route("/driver/battery-opt-status", methods=["POST"])
def driver_battery_opt_status():
    """Record whether this driver's phone has Cenas Kitchen whitelisted
    from battery optimization. The native plugin calls this at shift start
    with {granted: bool, prompted: bool} so partners can see who's
    whitelisted (GPS will keep streaming on screen-off) vs not.
    Sam #1025 2026-05-19."""
    driver_id = session.get("driver_id")
    if not driver_id:
        return jsonify({"error": "not signed in"}), 401
    data = request.get_json(silent=True) or {}
    granted = bool(data.get("granted"))
    db = next(get_db())
    try:
        d = db.get(Driver, driver_id)
        if d is None:
            return jsonify({"error": "driver not found"}), 404
        d.battery_opt_ignored = granted
        d.battery_opt_checked_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "granted": granted})
    finally:
        db.close()


@driver.route("/driver/track", methods=["POST"])
def driver_track():
    """Accept one GPS fix from the driver's phone. Body is JSON:
        {lat, lng, accuracy_m?, speed_mps?, heading_deg?}
    Requires the driver to be signed in AND have an open shift."""
    driver_id = session.get("driver_id")
    if not driver_id:
        return jsonify({"error": "not signed in"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        lat = float(payload["lat"])
        lng = float(payload["lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lng required"}), 400
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return jsonify({"error": "lat/lng out of range"}), 400
    db = next(get_db())
    try:
        open_shift = (db.query(DriverShift)
                      .filter(DriverShift.driver_id == driver_id,
                              DriverShift.ended_at.is_(None))
                      .order_by(DriverShift.started_at.desc())
                      .first())
        if not open_shift:
            return jsonify({"error": "no open shift — tap Start shift first"}), 409
        order_id = None
        raw_order_id = session.get("driver_active_order_id")
        if raw_order_id:
            active_order = db.get(Order, int(raw_order_id))
            if (
                active_order
                and active_order.assigned_driver_id == driver_id
                and active_order.status in {"approved", "picked_up", "en_route"}
            ):
                order_id = active_order.id
                active_order.tracking_status = "Tracked"
            else:
                session.pop("driver_active_order_id", None)
        loc = DriverLocation(
            shift_id=open_shift.id,
            driver_id=driver_id,
            order_id=order_id,
            lat=lat,
            lng=lng,
            accuracy_m=_safe_float(payload.get("accuracy_m")),
            speed_mps=_safe_float(payload.get("speed_mps")),
            heading_deg=_safe_float(payload.get("heading_deg")),
        )
        db.add(loc)
        d = db.get(Driver, driver_id)
        if d is not None:
            d.last_known_lat = lat
            d.last_known_lng = lng
            d.last_location_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "id": loc.id, "order_id": order_id})
    finally:
        db.close()


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def issue_temp_passcode(db, driver_row: Driver) -> str:
    """Generate a one-time 5-digit temp PIN, set it on the driver, return the
    plaintext for the manager to read aloud. Mirrors the User keypad reset
    pattern in app/web/team_routes.py:team_reset — sets first_login_done=False
    so the driver is forced to change it on first login, and bumps
    session_version so any active session is invalidated on next request.

    PIN uniqueness across drivers is no longer enforced (phone is the
    first factor at login as of 2026-05-13), so a simple random 5-digit
    temp is fine."""
    temp = _generate_temp_pin()
    driver_row.passcode_hash = generate_password_hash(temp)
    # Clear the legacy password_hash now that this driver is on the PIN flow.
    driver_row.password_hash = None
    driver_row.first_login_done = False
    driver_row.failed_attempts = 0
    driver_row.lockout_until = None
    driver_row.active = True
    driver_row.session_version = (driver_row.session_version or 1) + 1
    db.commit()
    return temp


# Back-compat alias: store_routes.drivers_reset (and any other older callers)
# import `issue_temp_password` directly. New code should call
# issue_temp_passcode. Same return value, same side effects.
issue_temp_password = issue_temp_passcode
