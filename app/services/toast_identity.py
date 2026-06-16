"""Employee-owned Toast identity helpers.

The old app source of truth was ``CenaToastLink``: one row per
(employee, store). The durable source is now the Employee row itself
(``toast_employee_guid`` + ``toast_employee_name``), with the link table kept as
a compatibility/display cache while existing screens are migrated.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re

from sqlalchemy import text


VALID_STORES = ("tomball", "copperfield")


@dataclass(frozen=True)
class EmployeeToastIdentity:
    cena_employee_id: int
    store_key: str
    toast_id: str
    toast_name: str | None = None


def normalize_name(name: str | None) -> str:
    clean = re.sub(r"[^a-z0-9 ]+", " ", (name or "").strip().casefold())
    return " ".join(clean.split())


def name_mismatch_warning(roster_name: str | None, toast_name: str | None) -> str | None:
    """Return a manager-facing warning when a linked roster name drifts.

    This is intentionally warning-only: nicknames and legal-name corrections are
    real. The important part is making the drift visible before it can affect
    sales/tip/labor attribution.
    """
    roster = normalize_name(roster_name)
    toast = normalize_name(toast_name)
    if not roster or not toast or roster == toast:
        return None
    if SequenceMatcher(None, roster, toast).ratio() >= 0.72:
        return None
    return (
        "Warning: roster name does not match linked Toast profile name "
        f"'{(toast_name or '').strip()}'. This may affect live sales and tip calculation."
    )


def set_employee_toast_identity(employee, toast_id: str | None, toast_name: str | None) -> None:
    employee.toast_employee_guid = (toast_id or "").strip() or None
    employee.toast_employee_name = (toast_name or "").strip() or None


def clear_employee_toast_identity(employee, toast_id: str | None = None) -> bool:
    current = (getattr(employee, "toast_employee_guid", None) or "").strip()
    if toast_id and current and current != str(toast_id).strip():
        return False
    employee.toast_employee_guid = None
    employee.toast_employee_name = None
    return True


def links_for_employee(db, employee) -> list[EmployeeToastIdentity]:
    """Return effective Toast identities for an employee.

    Employee.toast_employee_guid wins. If it is absent, fall back to legacy
    CenaToastLink rows so older data keeps working until audited.
    """
    from app.models import CenaToastLink, EmployeeStoreAssignment

    emp_id = int(getattr(employee, "id", 0) or 0)
    if emp_id <= 0:
        return []
    guid = (getattr(employee, "toast_employee_guid", None) or "").strip()
    if guid:
        stores = [
            (row.store_key or "").strip().lower()
            for row in db.query(EmployeeStoreAssignment.store_key)
                         .filter(EmployeeStoreAssignment.employee_id == emp_id)
                         .all()
        ]
        return [
            EmployeeToastIdentity(
                cena_employee_id=emp_id,
                store_key=store,
                toast_id=guid,
                toast_name=getattr(employee, "toast_employee_name", None),
            )
            for store in stores
            if store in VALID_STORES
        ]
    rows = (
        db.query(CenaToastLink)
          .filter(CenaToastLink.cena_employee_id == emp_id)
          .all()
    )
    return [
        EmployeeToastIdentity(
            cena_employee_id=emp_id,
            store_key=(row.store_key or "").strip().lower(),
            toast_id=(row.toast_id or "").strip(),
            toast_name=row.toast_name,
        )
        for row in rows
        if (row.store_key or "").strip().lower() in VALID_STORES
        and (row.toast_id or "").strip()
    ]


def identity_pairs_for_sync(db, only_store: str | None = None) -> list[tuple[str, str]]:
    """Distinct (store_key, toast_id) pairs for Toast snapshot/background sync."""
    from app.models import CenaToastLink, Employee, EmployeeStoreAssignment

    store_filter = (only_store or "").strip().lower() or None
    pairs: set[tuple[str, str]] = set()

    q = (
        db.query(EmployeeStoreAssignment.store_key, Employee.toast_employee_guid)
          .join(Employee, Employee.id == EmployeeStoreAssignment.employee_id)
          .filter(Employee.active.is_(True),
                  Employee.toast_employee_guid.isnot(None),
                  Employee.toast_employee_guid != "")
    )
    if store_filter:
        q = q.filter(EmployeeStoreAssignment.store_key == store_filter)
    for store, guid in q.distinct().all():
        s = (store or "").strip().lower()
        g = (guid or "").strip()
        if s in VALID_STORES and g:
            pairs.add((s, g))

    # Compatibility fallback: only use legacy rows for employees that do not yet
    # have the Employee-owned Toast GUID.
    legacy = (
        db.query(CenaToastLink.store_key, CenaToastLink.toast_id, Employee.toast_employee_guid)
          .outerjoin(Employee, Employee.id == CenaToastLink.cena_employee_id)
    )
    if store_filter:
        legacy = legacy.filter(CenaToastLink.store_key == store_filter)
    for store, guid, employee_guid in legacy.distinct().all():
        if (employee_guid or "").strip():
            continue
        s = (store or "").strip().lower()
        g = (guid or "").strip()
        if s in VALID_STORES and g:
            pairs.add((s, g))

    return sorted(pairs)


def linked_employee_store_keys(db) -> set[tuple[int, str]]:
    """Store eligibility pairs backed by Employee GUIDs, with legacy fallback."""
    from app.models import CenaToastLink, Employee, EmployeeStoreAssignment

    out: set[tuple[int, str]] = set()
    q = (
        db.query(Employee.id, EmployeeStoreAssignment.store_key)
          .join(EmployeeStoreAssignment,
                EmployeeStoreAssignment.employee_id == Employee.id)
          .filter(Employee.active.is_(True),
                  Employee.toast_employee_guid.isnot(None),
                  Employee.toast_employee_guid != "")
    )
    for emp_id, store in q.all():
        s = (store or "").strip().lower()
        if s in VALID_STORES:
            out.add((int(emp_id), s))

    for emp_id, store in db.query(CenaToastLink.cena_employee_id, CenaToastLink.store_key).all():
        s = (store or "").strip().lower()
        if s in VALID_STORES:
            out.add((int(emp_id), s))
    return out


def backfill_employee_toast_identity_from_links(engine) -> int:
    """One-time-safe boot backfill from legacy links.

    Only employees with exactly one distinct Toast GUID across all link rows are
    backfilled. Multiple distinct GUIDs are intentionally left blank for manual
    review in the Toast-link settings page.
    """
    if engine is None:
        return 0
    with engine.begin() as conn:
        rows = conn.execute(text(
            """
            SELECT
                cena_employee_id AS employee_id,
                MIN(TRIM(toast_id)) AS toast_id,
                MIN(NULLIF(TRIM(COALESCE(toast_name, '')), '')) AS toast_name,
                COUNT(DISTINCT TRIM(toast_id)) AS guid_count
            FROM cena_toast_link
            WHERE toast_id IS NOT NULL AND TRIM(toast_id) <> ''
            GROUP BY cena_employee_id
            HAVING COUNT(DISTINCT TRIM(toast_id)) = 1
            """
        )).mappings().all()
        updated = 0
        for row in rows:
            result = conn.execute(text(
                """
                UPDATE employees
                   SET toast_employee_guid = :toast_id,
                       toast_employee_name = COALESCE(:toast_name, toast_employee_name)
                 WHERE id = :employee_id
                   AND (toast_employee_guid IS NULL OR TRIM(toast_employee_guid) = '')
                """
            ), {
                "employee_id": row["employee_id"],
                "toast_id": row["toast_id"],
                "toast_name": row["toast_name"],
            })
            updated += int(getattr(result, "rowcount", 0) or 0)
        return updated
