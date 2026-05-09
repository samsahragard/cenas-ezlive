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

from flask import Blueprint, render_template, request, redirect, url_for, session
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

from app.db import get_db
from app.models import Driver, DriverLog

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


@driver.route("/driver/login", methods=["GET"])
def driver_login():
    return render_template("driver_login.html", error=None, prefill_email=request.args.get("email", ""))


@driver.route("/driver/login", methods=["POST"])
def driver_login_submit():
    email = _normalize_email(request.form.get("email"))
    password = request.form.get("password") or ""
    if not email or not password:
        return render_template("driver_login.html", error="Email and password required.",
                               prefill_email=email), 400
    db = next(get_db())
    try:
        found = db.query(Driver).filter(Driver.email == email).first()
        now = datetime.utcnow()
        if not found or not found.password_hash:
            return render_template("driver_login.html",
                                   error="No account found, or account hasn't been set up yet. "
                                         "Sign up below or contact your manager.",
                                   prefill_email=email), 401
        if not found.active:
            return render_template("driver_login.html",
                                   error="This account is deactivated. Contact your manager.",
                                   prefill_email=email), 403
        if found.lockout_until and found.lockout_until > now:
            mins = max(1, int((found.lockout_until - now).total_seconds() // 60) + 1)
            return render_template("driver_login.html",
                                   error=f"Too many failed attempts. Try again in {mins} min.",
                                   prefill_email=email), 429
        if not check_password_hash(found.password_hash, password):
            found.failed_attempts = (found.failed_attempts or 0) + 1
            if found.failed_attempts >= LOCKOUT_THRESHOLD:
                found.lockout_until = now + LOCKOUT_DURATION
            db.commit()
            return render_template("driver_login.html", error="Wrong password.",
                                   prefill_email=email), 401
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
        return render_template("driver_viewing.html", view="logs", driver=found,
                               location_label=LOCATION_LABELS.get(found.location, found.location),
                               logs=logs, error=None)
    finally:
        db.close()


@driver.route("/driver/logout", methods=["POST"])
def driver_logout():
    session.pop("driver_id", None)
    session.pop("driver_name", None)
    session.pop("driver_location", None)
    return redirect(url_for("driver.driver_login"))


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
