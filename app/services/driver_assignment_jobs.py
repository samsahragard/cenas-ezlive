"""Helpers for creating ezCater driver-assignment jobs.

Both the Ez Orders dropdown and the Ez Manage approval flow need to
produce the same DriverAssignmentJob row and wake the same pwck/aick
gateway. Keeping that contract here prevents the two surfaces from
drifting.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import Driver, DriverAssignmentJob, EzcaterKnownDriver, Order
from app.services.ezcater_known_drivers_seed import (
    fold_name,
    names_match,
    normalize_phone,
)

logger = logging.getLogger(__name__)

_ORIGIN_TO_CK_PREFIX = {
    "store_1": 1,
    "store_3": 1,
    "store_2": 2,
    "store_4": 2,
}
_LOCATION_TO_CK_PREFIX = {
    "copperfield": 1,
    "uno": 1,
    "tomball": 2,
    "dos": 2,
}


@dataclass(slots=True)
class AssignmentAlreadyInProgress(Exception):
    """Raised when a fresh pending/running job already exists."""

    job: DriverAssignmentJob


def _driver_prefix(driver: Driver, order: Order | None = None) -> int | None:
    if order is not None:
        prefix = _ORIGIN_TO_CK_PREFIX.get((order.origin_store_id or "").strip())
        if prefix is not None:
            return prefix
    return _LOCATION_TO_CK_PREFIX.get((driver.location or "").strip().lower())


def _candidate_roster(db: Session, prefix: int | None) -> list[EzcaterKnownDriver]:
    q = db.query(EzcaterKnownDriver)
    if prefix is not None:
        scoped = q.filter(EzcaterKnownDriver.ck_prefix == prefix).all()
        if scoped:
            return scoped
    return q.all()


def resolve_ezcater_driver_name(
    db: Session,
    driver: Driver,
    order: Order | None = None,
) -> str:
    """Return the canonical ezCater roster name for a local driver.

    Phone match wins. If the local profile is a shortened name such as
    "Tatiana", a unique same-kitchen roster token match resolves to
    "Tatiana Campos" so the ezCater modal receives the spelling it knows.
    """
    phone = normalize_phone(driver.phone or "")
    if phone:
        by_phone = (
            db.query(EzcaterKnownDriver)
            .filter(EzcaterKnownDriver.phone_e164 == phone)
            .first()
        )
        if by_phone:
            return by_phone.name

    candidates = _candidate_roster(db, _driver_prefix(driver, order))
    local_name = (driver.name or "").strip()
    if not local_name:
        return local_name

    for kd in candidates:
        if names_match(kd.name, local_name):
            return kd.name

    local_tokens = set(fold_name(local_name).split())
    if local_tokens:
        token_matches = []
        for kd in candidates:
            roster_tokens = set(fold_name(kd.name).split())
            if local_tokens and local_tokens.issubset(roster_tokens):
                token_matches.append(kd)
        if len(token_matches) == 1:
            return token_matches[0].name

    return local_name


def create_assignment_job(
    db: Session,
    *,
    order_id: str,
    current_driver: str | None,
    new_driver: str,
    idempotency_seconds: int = 5,
) -> DriverAssignmentJob:
    """Create a pending DriverAssignmentJob, enforcing the dropdown guard.

    The caller owns commit/rollback so this can be part of a larger approval
    transaction.
    """
    clean_order_id = (order_id or "").strip()
    clean_new_driver = (new_driver or "").strip()
    clean_current = (current_driver or "").strip() or None
    if not clean_order_id or not clean_new_driver:
        raise ValueError("order_id and new_driver are required")

    cutoff = datetime.utcnow() - timedelta(seconds=idempotency_seconds)
    existing = (
        db.query(DriverAssignmentJob)
        .filter(DriverAssignmentJob.order_id == clean_order_id)
        .filter(DriverAssignmentJob.created_at >= cutoff)
        .filter(DriverAssignmentJob.status.in_(("pending", "running")))
        .first()
    )
    if existing:
        raise AssignmentAlreadyInProgress(existing)

    job = DriverAssignmentJob(
        job_id=str(uuid.uuid4()),
        order_id=clean_order_id,
        current_driver=clean_current,
        new_driver=clean_new_driver,
        status="pending",
    )
    db.add(job)
    db.flush()
    return job


def wake_assignment_gateway(
    job_id: str,
    order_id: str,
    current_driver: str | None,
    new_driver: str,
) -> None:
    """Best-effort wake for pwck/aick after the DB transaction commits."""
    try:
        from app.services.ezcater_driver_assigner import dispatch_assignment_job

        dispatch_assignment_job(job_id, order_id, current_driver, new_driver)
    except Exception:
        logger.exception("wake_assignment_gateway: dispatch raised for job %s", job_id)
