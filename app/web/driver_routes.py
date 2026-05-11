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

import secrets
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

from app.db import get_db
from app.models import Driver, DriverLog, DriverShift, DriverLocation

driver = Blueprint("driver", __name__)

LOCATION_LABELS = {
    "copperfield": "Copperfield",
    "tomball": "Tomball",
}
LOCKOUT_THRESHOLD = 5
LOCKOUT_DURATION = timedelta(minutes=10)


def _normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


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
    return render_template("driver_login.html", error=None, prefill_email=request.args.get("email", ""))


@driver.route("/driver/login", methods=["POST"])
def driver_login_submit():
    raw = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    if not raw or not password:
        return render_template("driver_login.html", error="Email or phone, plus password, required.",
                               prefill_email=raw), 400
    # Sam (2026-05-10): accept either email or phone as the login id. If the
    # value contains digits but no '@', try the phone path first; otherwise
    # treat as email.
    from app.services.ezcater_known_drivers_seed import normalize_phone
    db = next(get_db())
    try:
        found = None
        if "@" in raw:
            found = db.query(Driver).filter(Driver.email == _normalize_email(raw)).first()
        else:
            digits = normalize_phone(raw)
            if digits:
                # Match against any Driver whose stored phone normalizes to the same digits.
                # Phones are stored loosely (free-text on signup), so we normalize on both sides.
                for d in db.query(Driver).filter(Driver.phone.isnot(None)).all():
                    if normalize_phone(d.phone) == digits:
                        found = d
                        break
        now = datetime.utcnow()
        if not found or not found.password_hash:
            return render_template("driver_login.html",
                                   error="No account found, or account hasn't been set up yet. "
                                         "Sign up below or contact your manager.",
                                   prefill_email=raw), 401
        if not found.active:
            return render_template("driver_login.html",
                                   error="This account is deactivated. Contact your manager.",
                                   prefill_email=raw), 403
        if found.lockout_until and found.lockout_until > now:
            mins = max(1, int((found.lockout_until - now).total_seconds() // 60) + 1)
            return render_template("driver_login.html",
                                   error=f"Too many failed attempts. Try again in {mins} min.",
                                   prefill_email=raw), 429
        if not check_password_hash(found.password_hash, password):
            found.failed_attempts = (found.failed_attempts or 0) + 1
            if found.failed_attempts >= LOCKOUT_THRESHOLD:
                found.lockout_until = now + LOCKOUT_DURATION
            db.commit()
            return render_template("driver_login.html", error="Wrong password.",
                                   prefill_email=raw), 401
        # Success — reset counters, set session
        found.failed_attempts = 0
        found.lockout_until = None
        db.commit()
        session["driver_id"] = found.id
        session["driver_name"] = found.name
        session["driver_location"] = found.location
        session.permanent = True
        return redirect(url_for("driver.driver_logs"))
    finally:
        db.close()


@driver.route("/driver/signup", methods=["GET"])
def driver_signup():
    return render_template("driver_signup.html", error=None, form={})


@driver.route("/driver/signup", methods=["POST"])
def driver_signup_submit():
    form = {
        "name": (request.form.get("name") or "").strip(),
        "email": _normalize_email(request.form.get("email")),
        "phone": (request.form.get("phone") or "").strip(),
        "address": (request.form.get("address") or "").strip(),
        "location": (request.form.get("location") or "").strip().lower(),
    }
    password = request.form.get("password") or ""
    confirm = request.form.get("password_confirm") or ""
    err = None
    if not form["name"] or not form["email"] or not password or not form["location"]:
        err = "Name, email, location, and password are required."
    elif form["location"] not in LOCATION_LABELS:
        err = "Pick a valid location (Copperfield or Tomball)."
    elif "@" not in form["email"] or "." not in form["email"].split("@")[-1]:
        err = "Enter a valid email."
    elif len(password) < 8:
        err = "Password must be at least 8 characters."
    elif password != confirm:
        err = "Passwords don't match."
    if err:
        return render_template("driver_signup.html", error=err, form=form), 400

    db = next(get_db())
    try:
        # Email must be globally unique across the system
        if db.query(Driver).filter(Driver.email == form["email"]).first():
            return render_template("driver_signup.html",
                                   error="That email is already registered. Use login instead.",
                                   form=form), 409
        new_driver = Driver(
            name=form["name"],
            location=form["location"],
            email=form["email"],
            phone=form["phone"] or None,
            address=form["address"] or None,
            password_hash=generate_password_hash(password),
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
        session["driver_id"] = new_driver.id
        session["driver_name"] = new_driver.name
        session["driver_location"] = new_driver.location
        session.permanent = True
        return redirect(url_for("driver.driver_logs"))
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


@driver.route("/driver/logout", methods=["POST"])
def driver_logout():
    session.pop("driver_id", None)
    session.pop("driver_name", None)
    session.pop("driver_location", None)
    return redirect(url_for("driver.driver_login"))


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
            db.commit()
            return jsonify({"ended_shift_id": open_shift.id})
        return jsonify({"ended_shift_id": None, "note": "no open shift"})
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
        loc = DriverLocation(
            shift_id=open_shift.id,
            driver_id=driver_id,
            lat=lat,
            lng=lng,
            accuracy_m=_safe_float(payload.get("accuracy_m")),
            speed_mps=_safe_float(payload.get("speed_mps")),
            heading_deg=_safe_float(payload.get("heading_deg")),
        )
        db.add(loc)
        db.commit()
        return jsonify({"ok": True, "id": loc.id})
    finally:
        db.close()


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def issue_temp_password(db, driver_row: Driver) -> str:
    """Generate a one-time temp password, set it on the driver, return the
    plaintext for the manager to read aloud. The manager UI is responsible
    for showing it exactly once."""
    temp = secrets.token_urlsafe(6)  # ~8 chars, URL-safe
    driver_row.password_hash = generate_password_hash(temp)
    driver_row.failed_attempts = 0
    driver_row.lockout_until = None
    driver_row.active = True
    db.commit()
    return temp
