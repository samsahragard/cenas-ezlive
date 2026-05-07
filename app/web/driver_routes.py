# API endpoints for routing/driver assignment
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, session
from app.db import get_db
from app.models import Driver, DriverLog

driver = Blueprint("driver", __name__)

LOCATION_LABELS = {
    "copperfield": "Copperfield",
    "tomball": "Tomball",
}


@driver.route("/driver", methods=["GET"])
def driver_login(): 
    return render_template("driver_viewing.html", view="login", error=None)


@driver.route("/driver/verify", methods=["POST"])
def driver_verify():
    name = request.form.get("name", "").strip()
    location = request.form.get("location", "").strip().lower()
    db = next(get_db())
    try:
        found = db.query(Driver).filter_by(name=name, location=location).first()
        if not found:
            return render_template("driver_viewing.html", view="login",
                                   error="No driver found with that name and location.")
        session["driver_name"] = found.name
        session["driver_location"] = found.location
        return redirect(url_for("driver.driver_logs"))
    finally:
        db.close()


@driver.route("/driver/logs", methods=["GET"])
def driver_logs():
    driver_name = session.get("driver_name")
    driver_location = session.get("driver_location")
    if not driver_name or not driver_location:
        return redirect(url_for("driver.driver_login"))
    db = next(get_db())
    try:
        found = db.query(Driver).filter_by(name=driver_name, location=driver_location).first()
        if not found:
            session.pop("driver_name", None)
            session.pop("driver_location", None)
            return redirect(url_for("driver.driver_login"))
        logs = (db.query(DriverLog)
                .filter_by(driver_name=found.name, location=driver_location)
                .order_by(DriverLog.pickup_date.desc())
                .all())
        return render_template("driver_viewing.html", view="logs", driver=found,
                               location_label=LOCATION_LABELS.get(driver_location, driver_location),
                               logs=logs, error=None)
    finally:
        db.close()


@driver.route("/driver/logout", methods=["POST"])
def driver_logout():
    session.pop("driver_name", None)
    session.pop("driver_location", None)
    return redirect(url_for("driver.driver_login"))
