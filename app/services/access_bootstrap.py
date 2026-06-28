"""Small requested access backfill for dashboard store badges.

The production shell is not guaranteed to have direct database credentials, so
these exact Team-user adjustments run idempotently at app boot after the users
table exists.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata

from app.models import Employee, EmployeePosition, EmployeeStoreAssignment, Position, User, UserAuditLog

log = logging.getLogger(__name__)

_UNCHANGED = object()
_ACTOR_LABEL = "system:access-bootstrap-2026-06-26"

_ROLE_TO_POSITION_NAME = {
    "corporate": "Corporate",
    "corporate_chef": "Corporate Chef",
    "gm": "GM",
    "km": "KM",
    "assistant_km": "Assistant KM",
    "foh_manager": "FOH Manager",
    "expo": "Expo",
}


@dataclass(frozen=True)
class AccessAssignment:
    aliases: tuple[str, ...]
    permission_level: str | object = _UNCHANGED
    store_scope: str | None | object = _UNCHANGED
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()


@dataclass(frozen=True)
class CopyAssignment:
    target_aliases: tuple[str, ...]
    source_aliases: tuple[str, ...]


@dataclass(frozen=True)
class ManagerProfileMove:
    employee_aliases: tuple[str, ...]
    template_user_aliases: tuple[str, ...]
    store_scope: str


FIXED_ASSIGNMENTS: tuple[AccessAssignment, ...] = (
    AccessAssignment(("Adriana Herrera",), store_scope="tomball"),
    AccessAssignment(("Angelica Barton",), permission_level="gm", store_scope="tomball,copperfield"),
    AccessAssignment(
        ("Sam Sahragard", "Sam"),
        permission_level="partner",
        store_scope=None,
        emails=("samsahragard@gmail.com", "sam@cenaskitchen.com"),
    ),
    AccessAssignment(
        ("Masood Sahragard", "Masood"),
        permission_level="partner",
        store_scope=None,
        emails=("masood@cenaskitchen.com",),
        phones=("8322832219",),
    ),
    AccessAssignment(("Janeth Arvizu Animas",), store_scope="tomball"),
    AccessAssignment(("Sebastian Ayala", "Sebastian"), store_scope="copperfield"),
)

COPY_ASSIGNMENTS: tuple[CopyAssignment, ...] = (
    CopyAssignment(("Ana Perez Albelo",), ("Tahily Vazquez",)),
    CopyAssignment(("Oneyda Martinez Orellana",), ("Tahily Vazquez",)),
)

MANAGER_PROFILE_MOVES: tuple[ManagerProfileMove, ...] = (
    ManagerProfileMove(
        ("Damon Greer", "Damon", "Damean", "Damian", "Damien", "Dameon", "Damen"),
        ("Adriana Herrera",),
        "copperfield",
    ),
    ManagerProfileMove(
        ("Alex Martinez Herrera", "Alex Martinez", "Alex"),
        ("Sebastian Ayala", "Sebastian"),
        "copperfield",
    ),
)


def _normalize_text(value: str | None) -> str:
    raw = unicodedata.normalize("NFKD", value or "")
    asciiish = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", asciiish.lower())).strip()


def _normalize_phone(value: str | None) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _role_state(user: User) -> str:
    return f"{user.permission_level or ''}|{user.store_scope or ''}"


def _single_active(hits: list) -> object | None:
    if len(hits) == 1:
        return hits[0]
    active_hits = [row for row in hits if getattr(row, "active", False)]
    if len(active_hits) == 1:
        return active_hits[0]
    return None


def _find_user(
    db,
    *,
    aliases: tuple[str, ...],
    emails: tuple[str, ...] = (),
    phones: tuple[str, ...] = (),
) -> User | None:
    users = db.query(User).all()

    wanted_emails = {_normalize_text(email) for email in emails if email}
    if wanted_emails:
        hits = [
            user for user in users
            if _normalize_text(getattr(user, "email", None)) in wanted_emails
        ]
        hit = _single_active(hits)
        if hit is not None:
            return hit

    wanted_phones = {_normalize_phone(phone) for phone in phones if phone}
    if wanted_phones:
        hits = [
            user for user in users
            if _normalize_phone(getattr(user, "phone", None)) in wanted_phones
        ]
        hit = _single_active(hits)
        if hit is not None:
            return hit

    normalized_names = [(user, _normalize_text(user.full_name)) for user in users]
    for alias in aliases:
        wanted = _normalize_text(alias)
        hits = [user for user, name in normalized_names if name == wanted]
        hit = _single_active(hits)
        if hit is not None:
            return hit

    for alias in aliases:
        wanted = _normalize_text(alias)
        if " " in wanted:
            continue
        hits = [
            user for user, name in normalized_names
            if name.split(" ", 1)[0] == wanted
        ]
        hit = _single_active(hits)
        if hit is not None:
            return hit

    log.info(
        "access bootstrap: skipped ambiguous/missing user aliases=%s emails=%s phones=%s",
        aliases,
        emails,
        phones,
    )
    return None


def _find_employee(db, *, aliases: tuple[str, ...]) -> Employee | None:
    employees = db.query(Employee).all()
    normalized_names = [(employee, _normalize_text(employee.full_name)) for employee in employees]
    for alias in aliases:
        wanted = _normalize_text(alias)
        hits = [employee for employee, name in normalized_names if name == wanted]
        hit = _single_active(hits)
        if hit is not None:
            return hit

    for alias in aliases:
        wanted = _normalize_text(alias)
        if " " in wanted:
            continue
        hits = [
            employee for employee, name in normalized_names
            if name.split(" ", 1)[0] == wanted
        ]
        hit = _single_active(hits)
        if hit is not None:
            return hit

    log.info("access bootstrap: skipped ambiguous/missing employee aliases=%s", aliases)
    return None


def _find_user_for_employee(db, employee: Employee) -> User | None:
    linked_id = getattr(employee, "user_id", None)
    if linked_id:
        linked = db.get(User, linked_id)
        if linked is not None:
            return linked

    phone = _normalize_phone(getattr(employee, "phone", None))
    if phone:
        hit = _single_active([
            user for user in db.query(User).filter(User.phone.isnot(None)).all()
            if _normalize_phone(user.phone) == phone
        ])
        if hit is not None:
            return hit

    email = _normalize_text(getattr(employee, "email", None))
    if email:
        hit = _single_active([
            user for user in db.query(User).filter(User.email.isnot(None)).all()
            if _normalize_text(user.email) == email
        ])
        if hit is not None:
            return hit

    name = _normalize_text(getattr(employee, "full_name", None))
    if name:
        hit = _single_active([
            user for user in db.query(User).all()
            if _normalize_text(user.full_name) == name
        ])
        if hit is not None:
            return hit

    return None


def _audit_role_change(db, user: User, before: str, details: str) -> None:
    db.add(UserAuditLog(
        target_user_id=user.id,
        target_label=user.full_name,
        actor_user_id=None,
        actor_label=_ACTOR_LABEL,
        action="role_change",
        before_value=before,
        after_value=_role_state(user),
        details=details,
        ip=None,
    ))


def _audit_user_create(db, user: User, details: str) -> None:
    db.add(UserAuditLog(
        target_user_id=user.id,
        target_label=user.full_name,
        actor_user_id=None,
        actor_label=_ACTOR_LABEL,
        action="create",
        before_value=None,
        after_value=_role_state(user),
        details=details,
        ip=None,
    ))


def _position_for_role(db, role: str) -> Position | None:
    position_name = _ROLE_TO_POSITION_NAME.get((role or "").strip().lower())
    if not position_name:
        return None

    matches = [
        row for row in db.query(Position).all()
        if _normalize_text(row.name) == _normalize_text(position_name)
    ]
    for row in matches:
        if getattr(row, "store_key", None) is None:
            return row
    if matches:
        return matches[0]

    row = Position(name=position_name, store_key=None)
    db.add(row)
    db.flush()
    return row


def _ensure_employee_manager_store_access(db, employee: Employee, role: str, store_scope: str) -> bool:
    changed = False

    assignment = (
        db.query(EmployeeStoreAssignment)
        .filter(
            EmployeeStoreAssignment.employee_id == employee.id,
            EmployeeStoreAssignment.store_key == store_scope,
        )
        .first()
    )
    if assignment is None:
        db.add(EmployeeStoreAssignment(employee_id=employee.id, store_key=store_scope))
        changed = True

    position = _position_for_role(db, role)
    if position is None:
        return changed

    employee_position = (
        db.query(EmployeePosition)
        .filter(
            EmployeePosition.employee_id == employee.id,
            EmployeePosition.position_id == position.id,
            EmployeePosition.store_key == store_scope,
        )
        .first()
    )
    if employee_position is None:
        db.add(EmployeePosition(
            employee_id=employee.id,
            position_id=position.id,
            store_key=store_scope,
        ))
        changed = True

    return changed


def _apply_access(
    db,
    user: User | None,
    *,
    permission_level: str | object = _UNCHANGED,
    store_scope: str | None | object = _UNCHANGED,
    details: str,
) -> bool:
    if user is None:
        return False

    before = _role_state(user)
    if permission_level is not _UNCHANGED:
        user.permission_level = str(permission_level)
    if store_scope is not _UNCHANGED:
        user.store_scope = store_scope

    after = _role_state(user)
    if before == after:
        return False

    user.session_version = (user.session_version or 0) + 1
    _audit_role_change(db, user, before, details)
    log.info("access bootstrap: %s %s -> %s", user.full_name, before, after)
    return True


def _move_employee_to_manager_profile(
    db,
    *,
    employee: Employee | None,
    template_user: User | None,
    store_scope: str,
) -> bool:
    if employee is None or template_user is None:
        return False
    role = (template_user.permission_level or "").strip()
    if not role:
        return False

    store_position_changed = _ensure_employee_manager_store_access(db, employee, role, store_scope)
    user = _find_user_for_employee(db, employee)
    created = False
    before = _role_state(user) if user is not None else None
    profile_before = None
    if user is not None:
        profile_before = (
            user.passcode_hash,
            user.active,
            user.first_login_done,
            user.failed_attempts,
            user.lockout_until,
            user.phone,
            user.email,
            user.full_name,
        )
    if user is None:
        passcode_hash = getattr(employee, "passcode_hash", None)
        if not passcode_hash:
            log.info(
                "access bootstrap: skipped manager profile move for %s; employee has no passcode_hash",
                getattr(employee, "full_name", "?"),
            )
            return False
        user = User(
            full_name=(getattr(employee, "full_name", None) or "").strip() or "Manager",
            email=(getattr(employee, "email", None) or None),
            phone=(getattr(employee, "phone", None) or None),
            passcode_hash=passcode_hash,
            permission_level=role,
            store_scope=store_scope,
            active=True,
            first_login_done=True,
            session_version=1,
        )
        db.add(user)
        db.flush()
        created = True
    else:
        user.permission_level = role
        user.store_scope = store_scope
        if getattr(employee, "passcode_hash", None):
            user.passcode_hash = employee.passcode_hash
        user.active = True
        user.first_login_done = True
        user.failed_attempts = 0
        user.lockout_until = None
        if not (user.phone or "").strip() and getattr(employee, "phone", None):
            user.phone = employee.phone
        if not (user.email or "").strip() and getattr(employee, "email", None):
            user.email = employee.email
        if not (user.full_name or "").strip() and getattr(employee, "full_name", None):
            user.full_name = employee.full_name

    employee_link_changed = getattr(employee, "user_id", None) != user.id
    if employee_link_changed:
        employee.user_id = user.id
        employee.session_version = (employee.session_version or 0) + 1

    after = _role_state(user)
    profile_after = (
        user.passcode_hash,
        user.active,
        user.first_login_done,
        user.failed_attempts,
        user.lockout_until,
        user.phone,
        user.email,
        user.full_name,
    )
    profile_changed = profile_before is not None and profile_before != profile_after
    user_changed = created or before != after or profile_changed
    if not created and user_changed:
        user.session_version = (user.session_version or 0) + 1

    if created:
        _audit_user_create(
            db,
            user,
            f"Moved employee profile to manager profile; copied access from {template_user.full_name}; store_scope={store_scope}.",
        )
    elif user_changed:
        details = (
            f"Moved employee profile to manager profile; copied access from {template_user.full_name}; "
            f"store_scope={store_scope}."
        )
        if before != after:
            _audit_role_change(db, user, before or "", details)
        else:
            db.add(UserAuditLog(
                target_user_id=user.id,
                target_label=user.full_name,
                actor_user_id=None,
                actor_label=_ACTOR_LABEL,
                action="edit",
                before_value=before,
                after_value=after,
                details=details,
                ip=None,
            ))

    if store_position_changed or user_changed or employee_link_changed:
        log.info(
            "access bootstrap: moved %s to manager user %s with %s",
            employee.full_name,
            user.id,
            after,
        )
        return True
    return False


def apply_requested_access_scopes(db) -> int:
    """Apply Sam's requested 2026-06-26 dashboard badge/access assignments.

    Returns the number of User rows changed. The caller owns commit/rollback.
    """
    changed = 0

    for assignment in FIXED_ASSIGNMENTS:
        user = _find_user(
            db,
            aliases=assignment.aliases,
            emails=assignment.emails,
            phones=assignment.phones,
        )
        if _apply_access(
            db,
            user,
            permission_level=assignment.permission_level,
            store_scope=assignment.store_scope,
            details="Requested dashboard badge/store access update.",
        ):
            changed += 1

    for assignment in COPY_ASSIGNMENTS:
        source = _find_user(db, aliases=assignment.source_aliases)
        target = _find_user(db, aliases=assignment.target_aliases)
        if source is None or target is None:
            continue
        if _apply_access(
            db,
            target,
            permission_level=source.permission_level,
            store_scope=source.store_scope,
            details=f"Copied dashboard view from {source.full_name}.",
        ):
            changed += 1

    for move in MANAGER_PROFILE_MOVES:
        employee = _find_employee(db, aliases=move.employee_aliases)
        template = _find_user(db, aliases=move.template_user_aliases)
        if _move_employee_to_manager_profile(
            db,
            employee=employee,
            template_user=template,
            store_scope=move.store_scope,
        ):
            changed += 1

    return changed
