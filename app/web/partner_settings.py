"""Partner settings pages."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime

from flask import Blueprint, redirect, render_template, request, session, url_for

from app.db import SessionLocal
from app.models import CenaToastLink, Employee, EmployeeStoreAssignment
from app.services.toast_client import ToastClient, restaurant_guids
from app.services.toast_identity import (
    clear_employee_toast_identity,
    name_mismatch_warning,
    set_employee_toast_identity,
)
from app.web.permissions import current_user_id, level_at_least, load_current_user

partner_settings_bp = Blueprint("partner_settings", __name__)

STORES = (
    {"key": "copperfield", "slug": "uno", "label": "Copperfield"},
    {"key": "tomball", "slug": "dos", "label": "Tomball"},
)
STORE_KEYS = {row["key"] for row in STORES}


@partner_settings_bp.before_request
def _partner_settings_gate():
    user = load_current_user()
    if user is None:
        nxt = request.full_path if request.full_path else request.path
        return redirect(url_for("keypad_auth.login", next=nxt))
    if not level_at_least(user.permission_level, "partner"):
        return ("Forbidden - partner settings are owner-only.", 403)
    if not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login"))
    return None


def _toast_name(row: dict) -> str:
    first = (row.get("firstName") or row.get("chosenName") or "").strip()
    last = (row.get("lastName") or "").strip()
    return (f"{first} {last}".strip()
            or row.get("email")
            or (row.get("guid") or "?")[:8])


def _toast_records(store: str) -> tuple[list[dict], str | None]:
    guid = restaurant_guids().get(store)
    if not guid:
        return [], f"No Toast restaurant GUID configured for {store}."
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(lambda: ToastClient.shared().fetch_employees(store, guid) or [])
        rows = future.result(timeout=8)
    except TimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        return [], "Toast employee lookup timed out. The roster still loaded; retry this page in a minute."
    except Exception as ex:
        executor.shutdown(wait=False, cancel_futures=True)
        return [], f"Toast employees unavailable: {ex}"
    executor.shutdown(wait=False)
    out = []
    for row in rows:
        if row.get("deleted"):
            continue
        toast_id = (row.get("guid") or "").strip()
        if not toast_id:
            continue
        out.append({"toast_id": toast_id, "name": _toast_name(row)})
    out.sort(key=lambda r: (r["name"].split()[0].casefold() if r["name"].split() else "",
                            r["name"].casefold()))
    return out, None


def _employee_for_store(db, employee_id: int, store: str) -> Employee | None:
    return (
        db.query(Employee)
          .join(EmployeeStoreAssignment,
                EmployeeStoreAssignment.employee_id == Employee.id)
          .filter(Employee.id == employee_id,
                  Employee.active.is_(True),
                  EmployeeStoreAssignment.store_key == store)
          .first()
    )


def _sync_link(db, employee: Employee, store: str, toast_id: str, toast_name: str | None) -> None:
    # Move any same-store duplicate legacy link away from the Toast GUID.
    (
        db.query(CenaToastLink)
          .filter(CenaToastLink.store_key == store,
                  CenaToastLink.toast_id == toast_id,
                  CenaToastLink.cena_employee_id != employee.id)
          .delete(synchronize_session=False)
    )
    # Keep the Employee source of truth one-to-one.
    for other in (
        db.query(Employee)
          .filter(Employee.id != employee.id,
                  Employee.toast_employee_guid == toast_id)
          .all()
    ):
        clear_employee_toast_identity(other, toast_id)

    row = (
        db.query(CenaToastLink)
          .filter_by(cena_employee_id=employee.id, store_key=store)
          .first()
    )
    now = datetime.utcnow()
    uid = current_user_id()
    if row is None:
        db.add(CenaToastLink(
            cena_employee_id=employee.id,
            store_key=store,
            toast_id=toast_id,
            toast_name=toast_name,
            confirmed_by=uid,
            confirmed_at=now,
        ))
    else:
        row.toast_id = toast_id
        row.toast_name = toast_name
        row.confirmed_by = uid
        row.confirmed_at = now
    set_employee_toast_identity(employee, toast_id, toast_name)


@partner_settings_bp.route("/partner/settings")
def partner_settings_index():
    return redirect(url_for("partner_settings.toast_links"))


@partner_settings_bp.route("/partner/settings/toast-links", methods=["GET"])
def toast_links():
    selected = (request.args.get("store") or "copperfield").strip().lower()
    if selected not in STORE_KEYS:
        selected = "copperfield"
    db = SessionLocal()
    try:
        stores = []
        for meta in STORES:
            store = meta["key"]
            if store == selected:
                toast_rows, toast_error = _toast_records(store)
            else:
                toast_rows, toast_error = [], None
            toast_by_id = {row["toast_id"]: row for row in toast_rows}
            links = {
                int(row.cena_employee_id): row
                for row in db.query(CenaToastLink)
                             .filter(CenaToastLink.store_key == store)
                             .all()
            }
            employees = (
                db.query(Employee)
                  .join(EmployeeStoreAssignment,
                        EmployeeStoreAssignment.employee_id == Employee.id)
                  .filter(EmployeeStoreAssignment.store_key == store,
                          Employee.active.is_(True))
                  .order_by(Employee.full_name)
                  .all()
            )
            roster_rows = []
            for emp in employees:
                legacy = links.get(int(emp.id))
                current_guid = (
                    (emp.toast_employee_guid or "").strip()
                    or ((legacy.toast_id or "").strip() if legacy else "")
                )
                current_name = (
                    (emp.toast_employee_name or "").strip()
                    or ((legacy.toast_name or "").strip() if legacy else "")
                    or ((toast_by_id.get(current_guid) or {}).get("name") if current_guid else "")
                )
                if emp.toast_employee_guid:
                    status = "Linked"
                elif legacy is not None:
                    status = "Legacy link"
                else:
                    status = "Unlinked"
                roster_rows.append({
                    "employee": emp,
                    "current_guid": current_guid,
                    "current_name": current_name,
                    "status": status,
                    "warning": name_mismatch_warning(emp.full_name, current_name),
                })
            stores.append({
                **meta,
                "selected": store == selected,
                "toast_rows": toast_rows,
                "toast_error": toast_error,
                "roster_rows": roster_rows,
            })
        return render_template(
            "partner_toast_links.html",
            stores=stores,
            selected_store=selected,
        )
    finally:
        db.close()


@partner_settings_bp.route("/partner/settings/toast-links/link", methods=["POST"])
def toast_links_link():
    store = (request.form.get("store_key") or "").strip().lower()
    if store not in STORE_KEYS:
        return ("Unknown store.", 400)
    try:
        employee_id = int(request.form.get("employee_id") or "")
    except (TypeError, ValueError):
        return ("Employee required.", 400)
    toast_id = (request.form.get("toast_employee_guid") or "").strip()
    if not toast_id:
        return ("Toast employee required.", 400)

    toast_rows, _toast_error = _toast_records(store)
    toast_name = next((row["name"] for row in toast_rows if row["toast_id"] == toast_id), None)
    toast_name = toast_name or (request.form.get("toast_employee_name") or "").strip() or None

    db = SessionLocal()
    try:
        emp = _employee_for_store(db, employee_id, store)
        if emp is None:
            return ("That profile is not active at this store.", 400)
        _sync_link(db, emp, store, toast_id, toast_name)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return redirect(url_for("partner_settings.toast_links", store=store))


@partner_settings_bp.route("/partner/settings/toast-links/unlink", methods=["POST"])
def toast_links_unlink():
    store = (request.form.get("store_key") or "").strip().lower()
    if store not in STORE_KEYS:
        return ("Unknown store.", 400)
    try:
        employee_id = int(request.form.get("employee_id") or "")
    except (TypeError, ValueError):
        return ("Employee required.", 400)
    db = SessionLocal()
    try:
        row = (
            db.query(CenaToastLink)
              .filter_by(cena_employee_id=employee_id, store_key=store)
              .first()
        )
        old_toast_id = row.toast_id if row is not None else None
        (
            db.query(CenaToastLink)
              .filter_by(cena_employee_id=employee_id, store_key=store)
              .delete(synchronize_session=False)
        )
        emp = db.get(Employee, employee_id)
        if emp is not None:
            clear_employee_toast_identity(emp, old_toast_id)
        db.commit()
    finally:
        db.close()
    return redirect(url_for("partner_settings.toast_links", store=store))
