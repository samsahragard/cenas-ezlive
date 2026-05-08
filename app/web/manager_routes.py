from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, session
from app.db import get_db
from app.models import Driver, DriverLog

manager = Blueprint("manager", __name__)

LOCATION_LABELS = {
    "copperfield": "Copperfield",
    "tomball": "Tomball",
    "corporate": "Corporate",
}


def _get_location():
    return session.get("manager_location")


@manager.route("/manager", methods=["GET"])
def manager_login():
    return render_template("manager_logging.html", view="login", error=None)


@manager.route("/manager/verify", methods=["POST"])
def manager_verify():
    location = request.form.get("location", "").strip().lower()

    if location not in LOCATION_LABELS:
        return render_template("manager_logging.html", view="login",
                               error="Invalid location.")

    session["manager_location"] = location
    return redirect(url_for("manager.manager_log"))


@manager.route("/manager/logout", methods=["POST"])
def manager_logout():
    session.pop("manager_location", None)
    return redirect(url_for("manager.manager_login"))


@manager.route("/driver-logs", methods=["GET", "POST"])
def manager_log():
    location = _get_location()
    if not location:
        return redirect(url_for("manager.manager_login"))

    location_label = LOCATION_LABELS[location]
    is_corporate = location == "corporate"
    drivers = []
    logs = []
    db = next(get_db())
    try:
        if is_corporate:
            drivers = db.query(Driver).order_by(Driver.location, Driver.name).all()
            logs = db.query(DriverLog).order_by(DriverLog.location, DriverLog.pickup_date.desc()).all()
        else:
            drivers = db.query(Driver).filter_by(location=location).order_by(Driver.name).all()
            logs = db.query(DriverLog).filter_by(location=location).order_by(DriverLog.pickup_date.desc()).all()

        if request.method == "GET" or is_corporate:
            return render_template("manager_logging.html", view="dashboard",
                                   drivers=drivers, logs=logs,
                                   location=location, location_label=location_label,
                                   is_corporate=is_corporate,
                                   success=None, error=None)

        driver_name = request.form.get("driver_name", "").strip()
        order_link = request.form.get("order_link", "").strip() or None
        pickup_date = request.form.get("pickup_date", "").strip() or None
        ex_miles = request.form.get("ex_miles", "").strip() or None
        ex_miles_verified = request.form.get("ex_miles_verified", "").strip() or None
        on_time = bool(request.form.get("on_time"))
        tracking = bool(request.form.get("tracking"))
        picture = bool(request.form.get("picture"))
        five_star = bool(request.form.get("five_star"))
        notes = request.form.get("notes", "").strip() or None
        logged_by = request.form.get("logged_by", "").strip() or None

        if not driver_name:
            return render_template("manager_logging.html", view="dashboard",
                                   drivers=drivers, logs=logs,
                                   location=location, location_label=location_label,
                                   is_corporate=is_corporate,
                                   success=None, error="Driver Name is Required")

        driver = db.query(Driver).filter_by(name=driver_name, location=location).first()
        if not driver:
            return render_template("manager_logging.html", view="dashboard",
                                   drivers=drivers, logs=logs,
                                   location=location, location_label=location_label,
                                   is_corporate=is_corporate,
                                   success=None, error=f"Driver '{driver_name}' not found")

        db.add(DriverLog(
            driver_name=driver.name,
            location=location,
            order_link=order_link,
            pickup_date=pickup_date,
            ex_miles=int(ex_miles) if ex_miles else None,
            ex_miles_verified=int(ex_miles_verified) if ex_miles_verified else None,
            on_time=on_time,
            tracking=tracking,
            picture=picture,
            five_star=five_star,
            notes=notes,
            logged_by=logged_by,
        ))
        db.commit()
        return redirect(url_for("manager.manager_log", saved="1"))

    except Exception as e:
        db.rollback()
        return render_template("manager_logging.html", view="dashboard",
                               drivers=drivers, logs=logs,
                               location=location, location_label=location_label,
                               is_corporate=is_corporate,
                               success=None, error=f"Unexpected Error: {e}")
    finally:
        db.close()


@manager.route("/drivers/add", methods=["POST"])
def add_driver():
    location = _get_location()
    if not location:
        return redirect(url_for("manager.manager_login"))
    if location == "corporate":
        return redirect(url_for("manager.manager_log"))

    location_label = LOCATION_LABELS[location]
    drivers = []
    logs = []
    db = next(get_db())
    try:
        name = request.form.get("driver_name", "").strip()

        drivers = db.query(Driver).filter_by(location=location).order_by(Driver.name).all()
        logs = db.query(DriverLog).filter_by(location=location).order_by(DriverLog.pickup_date.desc()).all()

        if not name:
            return render_template("manager_logging.html", view="dashboard",
                                   drivers=drivers, logs=logs,
                                   location=location, location_label=location_label,
                                   is_corporate=False,
                                   success=None, error="Driver name is required.")
        if db.query(Driver).filter_by(name=name, location=location).first():
            return render_template("manager_logging.html", view="dashboard",
                                   drivers=drivers, logs=logs,
                                   location=location, location_label=location_label,
                                   is_corporate=False,
                                   success=None, error=f"Driver '{name}' already exists.")

        db.add(Driver(name=name, location=location))
        db.commit()
        return redirect(url_for("manager.manager_log", driver_added="1"))

    except Exception as e:
        db.rollback()
        return render_template("manager_logging.html", view="dashboard",
                               drivers=drivers, logs=logs,
                               location=location, location_label=location_label,
                               is_corporate=False,
                               success=None, error=f"Unexpected Error: {e}")
    finally:
        db.close()

@manager.route("/drivers/delete/<int:driver_id>", methods=["POST"])
def delete_driver(driver_id):
    location = _get_location()
    if not location:
        return redirect(url_for("manager.manager_login"))
    if location == "corporate":
        return redirect(url_for("manager.manager_log"))

    location_label = LOCATION_LABELS[location]
    db = next(get_db())
    try:
        driver = db.query(Driver).filter_by(id=driver_id, location=location).first()
        if not driver:
            drivers = db.query(Driver).filter_by(location=location).order_by(Driver.name).all()
            logs = db.query(DriverLog).filter_by(location=location).order_by(DriverLog.pickup_date.desc()).all()
            return render_template("manager_logging.html", view="dashboard",
                                   drivers=drivers, logs=logs,
                                   location=location, location_label=location_label,
                                   is_corporate=False,
                                   success=None, error="Driver not found.")

        has_logs = db.query(DriverLog).filter_by(driver_name=driver.name, location=location).first()
        if has_logs:
            drivers = db.query(Driver).filter_by(location=location).order_by(Driver.name).all()
            logs = db.query(DriverLog).filter_by(location=location).order_by(DriverLog.pickup_date.desc()).all()
            return render_template("manager_logging.html", view="dashboard",
                                   drivers=drivers, logs=logs,
                                   location=location, location_label=location_label,
                                   is_corporate=False,
                                   success=None, error=f"Cannot delete '{driver.name}' — they have existing log entries.")

        db.delete(driver)
        db.commit()
        return redirect(url_for("manager.manager_log", driver_deleted="1"))

    except Exception as e:
        db.rollback()
        return redirect(url_for("manager.manager_log"))
    finally:
        db.close()
