"""Small requested access backfill for dashboard store badges.

The production shell is not guaranteed to have direct database credentials, so
these exact Team-user adjustments run idempotently at app boot after the users
table exists. It does not create accounts or touch passcodes.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata

from app.models import User, UserAuditLog

log = logging.getLogger(__name__)

_UNCHANGED = object()
_ACTOR_LABEL = "system:access-bootstrap-2026-06-26"


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


def _normalize_text(value: str | None) -> str:
    raw = unicodedata.normalize("NFKD", value or "")
    asciiish = "".join(ch for ch in raw if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", asciiish.lower())).strip()


def _normalize_phone(value: str | None) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _role_state(user: User) -> str:
    return f"{user.permission_level or ''}|{user.store_scope or ''}"


def _single_user(hits: list[User]) -> User | None:
    if len(hits) == 1:
        return hits[0]
    active_hits = [user for user in hits if getattr(user, "active", False)]
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
        hit = _single_user(hits)
        if hit is not None:
            return hit

    wanted_phones = {_normalize_phone(phone) for phone in phones if phone}
    if wanted_phones:
        hits = [
            user for user in users
            if _normalize_phone(getattr(user, "phone", None)) in wanted_phones
        ]
        hit = _single_user(hits)
        if hit is not None:
            return hit

    normalized_names = [(user, _normalize_text(user.full_name)) for user in users]
    for alias in aliases:
        wanted = _normalize_text(alias)
        hits = [user for user, name in normalized_names if name == wanted]
        hit = _single_user(hits)
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
        hit = _single_user(hits)
        if hit is not None:
            return hit

    log.info(
        "access bootstrap: skipped ambiguous/missing user aliases=%s emails=%s phones=%s",
        aliases,
        emails,
        phones,
    )
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

    return changed
