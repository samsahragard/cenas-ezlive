from __future__ import annotations

from app.models import User, UserAuditLog
from app.services.access_bootstrap import apply_requested_access_scopes


def _user(
    db,
    uid: int,
    name: str,
    *,
    role: str = "expo",
    scope: str | None = "copperfield",
    email: str | None = None,
    phone: str | None = None,
) -> User:
    row = User(
        id=uid,
        full_name=name,
        email=email or f"user{uid}@test.local",
        phone=phone or f"555200{uid:04d}",
        passcode_hash="test-hash",
        permission_level=role,
        store_scope=scope,
        active=True,
        first_login_done=True,
        session_version=1,
    )
    db.add(row)
    return row


def test_requested_access_bootstrap_updates_named_users_and_is_idempotent(db_session):
    adriana = _user(db_session, 1, "Adriana Herrera", role="foh_manager", scope="copperfield")
    angelica = _user(db_session, 2, "Angelica Barton", role="corporate", scope=None)
    sam = _user(db_session, 3, "Sam Sahragard", role="gm", scope="tomball", email="sam@cenaskitchen.com")
    masood = _user(db_session, 4, "Masood Sahragard", role="gm", scope="copperfield", phone="832-283-2219")
    janeth = _user(db_session, 5, "Janeth Arvizu Animas", role="km", scope="copperfield")
    sebastian = _user(db_session, 6, "Sebastian Ayala", role="km", scope="tomball")
    tahily = _user(db_session, 7, "Tahily Vazquez", role="foh_manager", scope="copperfield")
    ana = _user(db_session, 8, "Ana Perez Albelo", role="expo", scope="tomball")
    oneyda = _user(db_session, 9, "Oneyda Martinez Orellana", role="expo", scope="tomball")
    db_session.commit()

    changed = apply_requested_access_scopes(db_session)
    db_session.commit()

    assert changed == 8
    assert (adriana.permission_level, adriana.store_scope) == ("foh_manager", "tomball")
    assert (angelica.permission_level, angelica.store_scope) == ("gm", "tomball,copperfield")
    assert (sam.permission_level, sam.store_scope) == ("partner", None)
    assert (masood.permission_level, masood.store_scope) == ("partner", None)
    assert (janeth.permission_level, janeth.store_scope) == ("km", "tomball")
    assert (sebastian.permission_level, sebastian.store_scope) == ("km", "copperfield")
    assert (ana.permission_level, ana.store_scope) == (tahily.permission_level, tahily.store_scope)
    assert (oneyda.permission_level, oneyda.store_scope) == (tahily.permission_level, tahily.store_scope)
    assert db_session.query(UserAuditLog).count() == 8
    assert sam.session_version == 2

    changed_again = apply_requested_access_scopes(db_session)
    db_session.commit()

    assert changed_again == 0
    assert db_session.query(UserAuditLog).count() == 8
