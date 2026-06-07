from __future__ import annotations

import pytest

from app.models import Driver, EzcaterKnownDriver, Order
from app.services.driver_assignment_jobs import (
    AssignmentAlreadyInProgress,
    create_assignment_job,
    resolve_ezcater_driver_name,
)


def test_resolve_ezcater_driver_name_uses_unique_roster_name(db_session):
    db_session.add(EzcaterKnownDriver(
        name="Tatiana Campos",
        phone_e164="3464689339",
        ck_prefix=2,
    ))
    driver = Driver(name="Tatiana", location="tomball", phone=None, active=True)
    order = Order(external_order_id="TAT-1", origin_store_id="store_2")
    db_session.add_all([driver, order])
    db_session.flush()

    assert resolve_ezcater_driver_name(db_session, driver, order) == "Tatiana Campos"


def test_resolve_ezcater_driver_name_phone_wins(db_session):
    db_session.add(EzcaterKnownDriver(
        name="Tatiana Campos",
        phone_e164="3464689339",
        ck_prefix=2,
    ))
    driver = Driver(
        name="T Campos",
        location="tomball",
        phone="(346) 468-9339",
        active=True,
    )
    db_session.add(driver)
    db_session.flush()

    assert resolve_ezcater_driver_name(db_session, driver) == "Tatiana Campos"


def test_create_assignment_job_blocks_fresh_duplicate(db_session):
    first = create_assignment_job(
        db_session,
        order_id="922-QET",
        current_driver=None,
        new_driver="Tatiana Campos",
    )
    db_session.flush()

    with pytest.raises(AssignmentAlreadyInProgress) as exc:
        create_assignment_job(
            db_session,
            order_id="922-QET",
            current_driver=None,
            new_driver="Tatiana Campos",
        )

    assert exc.value.job.job_id == first.job_id
