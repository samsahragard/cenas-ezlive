"""Toast employee -> Cenas employee profile reconciliation.

The Team > Link tab can see Toast-only people, but a page render should not be
responsible for creating app profiles. This service runs from the Toast sync
path instead: fetch fresh Toast employees for each configured restaurant, create
or reuse the matching Cenas Employee, assign that Employee to the store, then
persist a CenaToastLink so the person stops showing as "Toast only".
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.db import SessionLocal
from app.services.ezcater_known_drivers_seed import normalize_phone
from app.services.toast_client import ToastClient, restaurant_guids

log = logging.getLogger(__name__)

_VALID_STORES = {"tomball", "copperfield"}
_STORE_ALIASES = {
    "dos": "tomball",
    "ck2": "tomball",
    "tomball": "tomball",
    "uno": "copperfield",
    "ck1": "copperfield",
    "copperfield": "copperfield",
}


def _store_key(raw: str | None) -> str | None:
    key = (raw or "").strip().lower()
    if not key:
        return None
    return _STORE_ALIASES.get(key, key)


def _name_key(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def _toast_name(row: dict) -> str:
    first = (row.get("firstName") or row.get("chosenName") or "").strip()
    last = (row.get("lastName") or "").strip()
    return (f"{first} {last}".strip()
            or (row.get("email") or "").strip()
            or str(row.get("guid") or "")[:8]
            or "Toast employee")


def _toast_email(row: dict) -> str | None:
    email = str(row.get("email") or "").strip()
    return email or None


def _toast_phone(row: dict) -> str:
    raw = str(row.get("phoneNumber") or "").strip()
    cc = str(row.get("phoneNumberCountryCode") or "").strip()
    if cc and raw and not raw.startswith(cc):
        raw = f"{cc}{raw}"
    digits = normalize_phone(raw)
    return digits if len(digits) == 10 else ""


def _fetch_employees(client, store: str, guid: str) -> list[dict]:
    """Always ask Toast for a fresh employee list when reconciling profiles.

    Tests use small fake clients that do not accept ``refresh``; support both.
    """
    try:
        return list(client.fetch_employees(store, guid, refresh=True) or [])
    except TypeError:
        return list(client.fetch_employees(store, guid) or [])


def _ignored_ids(db, store: str) -> tuple[set[int], set[str]]:
    from app.models import CenaToastIgnore

    ignored_cena: set[int] = set()
    ignored_toast: set[str] = set()
    rows = (db.query(CenaToastIgnore.source, CenaToastIgnore.source_id)
              .filter(CenaToastIgnore.store_key == store)
              .all())
    for source, source_id in rows:
        sid = str(source_id or "").strip()
        if not sid:
            continue
        if source == "toast":
            ignored_toast.add(sid)
        elif source == "cena":
            try:
                ignored_cena.add(int(sid))
            except (TypeError, ValueError):
                continue
    return ignored_cena, ignored_toast


def _store_assignment_exists(db, employee_id: int, store: str) -> bool:
    from app.models import EmployeeStoreAssignment

    return (db.query(EmployeeStoreAssignment.id)
              .filter(EmployeeStoreAssignment.employee_id == employee_id,
                      EmployeeStoreAssignment.store_key == store)
              .first()) is not None


def _ensure_store_assignment(db, employee_id: int, store: str) -> bool:
    from app.models import EmployeeStoreAssignment

    if _store_assignment_exists(db, employee_id, store):
        return False
    db.add(EmployeeStoreAssignment(employee_id=employee_id, store_key=store))
    return True


def _phone_available(db, digits: str) -> bool:
    from app.models import Employee

    if not digits:
        return False
    for existing_phone, in db.query(Employee.phone).filter(Employee.phone.isnot(None)).all():
        if normalize_phone(existing_phone or "") == digits:
            return False
    return True


def _linked_to_other_toast(db, employee_id: int, store: str, toast_id: str) -> bool:
    from app.models import CenaToastLink

    row = (db.query(CenaToastLink)
             .filter(CenaToastLink.cena_employee_id == employee_id,
                     CenaToastLink.store_key == store)
             .first())
    return row is not None and str(row.toast_id or "") != toast_id


def _unique_candidate(candidates: list) -> object | None:
    by_id = {getattr(row, "id", None): row for row in candidates if getattr(row, "id", None) is not None}
    return next(iter(by_id.values())) if len(by_id) == 1 else None


def _find_existing_employee(
    db,
    *,
    store: str,
    toast_id: str,
    name: str,
    phone: str,
    email: str | None,
    ignored_cena: set[int],
):
    """Return (Employee, match_reason) when there is one safe local match."""
    from app.models import Employee, EmployeeStoreAssignment

    def _usable(rows):
        return [
            row for row in rows
            if row.id not in ignored_cena
            and not _linked_to_other_toast(db, row.id, store, toast_id)
        ]

    if phone:
        rows = [
            row for row in db.query(Employee).filter(Employee.active.is_(True)).all()
            if normalize_phone(row.phone or "") == phone
        ]
        hit = _unique_candidate(_usable(rows))
        if hit is not None:
            return hit, "phone"

    if email:
        email_lc = email.lower()
        rows = [
            row for row in db.query(Employee).filter(Employee.active.is_(True)).all()
            if (row.email or "").strip().lower() == email_lc
        ]
        hit = _unique_candidate(_usable(rows))
        if hit is not None:
            return hit, "email"

    key = _name_key(name)
    if key:
        rows = (
            db.query(Employee)
              .join(EmployeeStoreAssignment,
                    EmployeeStoreAssignment.employee_id == Employee.id)
              .filter(Employee.active.is_(True),
                      EmployeeStoreAssignment.store_key == store)
              .all()
        )
        rows = [row for row in rows if _name_key(row.full_name) == key]
        hit = _unique_candidate(_usable(rows))
        if hit is not None:
            return hit, "name"

    return None, None


def _upsert_created_profile_signal(
    db,
    *,
    store: str,
    toast_id: str,
    toast_name: str,
    employee_id: int,
    match_reason: str,
) -> None:
    from app.models import Signal

    subject_id = f"{store}:{toast_id}"
    row = (db.query(Signal)
             .filter(Signal.rule_name == "labor.toast_employee_profile_created",
                     Signal.subject_id == subject_id,
                     Signal.store_id == store,
                     Signal.resolved_at.is_(None),
                     Signal.acknowledged_at.is_(None))
             .first())
    payload = {
        "store": store,
        "toast_id": toast_id,
        "toast_name": toast_name,
        "employee_id": employee_id,
        "match_reason": match_reason,
    }
    if row is None:
        db.add(Signal(
            rule_name="labor.toast_employee_profile_created",
            severity="info",
            store_id=store,
            subject_id=subject_id,
            subject_label=f"Toast profile created: {toast_name}",
            trigger_at=datetime.utcnow(),
            payload=payload,
            action_text="Cenas created and linked this Toast employee profile automatically. Add position/passcode if needed.",
            surfaces=["home", "partner.anomalies", "morning_brief"],
            audience_roles=["partner", "corporate", "gm"],
        ))
    else:
        row.trigger_at = datetime.utcnow()
        row.subject_label = f"Toast profile created: {toast_name}"
        row.payload = payload


def reconcile_toast_employee_profiles(only_store: str | None = None, *, client=None, db=None) -> dict:
    """Create/link Cenas employee profiles for Toast employees in each store.

    Returns a compact summary and never raises for normal Toast/DB row failures;
    callers can log or surface the returned ``errors`` list.
    """
    from app.models import CenaToastLink, Employee
    from app.services.toast_identity import set_employee_toast_identity

    store_filter = _store_key(only_store)
    owns_db = db is None
    db = db or SessionLocal()
    toast = client or ToastClient.shared()
    summary = {
        "stores": {},
        "seen": 0,
        "created": 0,
        "reused": 0,
        "assigned": 0,
        "linked": 0,
        "skipped": 0,
        "errors": [],
    }
    try:
        guids = restaurant_guids()
        stores = [s for s in sorted(guids) if s in _VALID_STORES]
        if store_filter:
            stores = [s for s in stores if s == store_filter]
            if not stores:
                summary["errors"].append({"store": store_filter, "error": "No Toast restaurant GUID configured."})
                return summary

        for store in stores:
            result = {
                "seen": 0,
                "created": 0,
                "reused": 0,
                "assigned": 0,
                "linked": 0,
                "skipped": 0,
                "errors": [],
            }
            summary["stores"][store] = result
            try:
                toast_rows = _fetch_employees(toast, store, guids[store])
            except Exception as ex:
                msg = f"{type(ex).__name__}: {ex}"
                result["errors"].append(msg)
                summary["errors"].append({"store": store, "error": msg})
                continue

            ignored_cena, ignored_toast = _ignored_ids(db, store)
            for row in toast_rows:
                if row.get("deleted"):
                    continue
                toast_id = str(row.get("guid") or "").strip()
                if not toast_id:
                    result["skipped"] += 1
                    continue
                result["seen"] += 1
                summary["seen"] += 1
                toast_name = _toast_name(row)
                if toast_id in ignored_toast:
                    result["skipped"] += 1
                    summary["skipped"] += 1
                    continue
                already = (db.query(CenaToastLink)
                             .filter(CenaToastLink.store_key == store,
                                     CenaToastLink.toast_id == toast_id)
                             .first())
                if already is not None:
                    emp = db.get(Employee, already.cena_employee_id)
                    if emp is not None and not (emp.toast_employee_guid or "").strip():
                        set_employee_toast_identity(emp, toast_id, toast_name)
                        try:
                            db.commit()
                        except Exception:
                            db.rollback()
                    result["skipped"] += 1
                    summary["skipped"] += 1
                    continue

                phone = _toast_phone(row)
                email = _toast_email(row)
                try:
                    employee, reason = _find_existing_employee(
                        db,
                        store=store,
                        toast_id=toast_id,
                        name=toast_name,
                        phone=phone,
                        email=email,
                        ignored_cena=ignored_cena,
                    )
                    created = False
                    if employee is None:
                        employee = Employee(
                            full_name=toast_name,
                            phone=(phone if _phone_available(db, phone) else None),
                            email=email,
                            active=True,
                            session_version=1,
                        )
                        db.add(employee)
                        db.flush()
                        reason = "created"
                        created = True

                    assigned = _ensure_store_assignment(db, int(employee.id), store)
                    set_employee_toast_identity(employee, toast_id, toast_name)
                    db.add(CenaToastLink(
                        cena_employee_id=int(employee.id),
                        store_key=store,
                        toast_id=toast_id,
                        toast_name=toast_name,
                        confirmed_by=None,
                        confirmed_at=datetime.utcnow(),
                    ))
                    if created:
                        _upsert_created_profile_signal(
                            db,
                            store=store,
                            toast_id=toast_id,
                            toast_name=toast_name,
                            employee_id=int(employee.id),
                            match_reason=str(reason or "created"),
                        )
                    db.commit()

                    if created:
                        result["created"] += 1
                        summary["created"] += 1
                    else:
                        result["reused"] += 1
                        summary["reused"] += 1
                    if assigned:
                        result["assigned"] += 1
                        summary["assigned"] += 1
                    result["linked"] += 1
                    summary["linked"] += 1
                except Exception as ex:
                    db.rollback()
                    msg = f"{toast_name}: {type(ex).__name__}: {ex}"
                    result["errors"].append(msg)
                    summary["errors"].append({"store": store, "toast_id": toast_id, "error": msg})
                    result["skipped"] += 1
                    summary["skipped"] += 1

        log.info("toast-profile-reconcile: %s", summary)
        return summary
    finally:
        if owns_db:
            db.close()
