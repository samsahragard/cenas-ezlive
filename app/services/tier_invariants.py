"""Tier-invariant GUARD module for the team-roster store split (ck, S6a).

Pure, config-driven guards that enforce two of Sam's CONFIRMED structural
decisions for the top tier of the permission_catalog. This module is the
single place those two invariants live; it deliberately does NOT wire itself
into any endpoint (that is a later wave) and it touches NO database / Flask
request state at import OR at call time -- every public function is a pure
function over plain values (a User-like object or an email string).

The two invariants (Sam's confirmed decisions):

  1. PARTNER is EXACTLY TWO fixed identities -- Sam + Masood -- pinned by
     login email (case-insensitive). Nobody else may ever become a partner,
     and a pinned partner may never be demoted / removed. This is the
     "hard-block + escape hatch" decision.

  2. CORPORATE is the top tier across BOTH stores: a corporate user MUST have
     store_scope NULL (== all stores). A corporate user pinned to a single
     store is a contradiction and is rejected.

How identity is represented (matched to app.models.User, verified S6a):
  * permission_level : str  -- 'partner' | 'corporate' | 'gm' | ... (the same
                              vocabulary as permission_catalog.ROLES keys and
                              permissions.ROLE_PERMISSIONS).
  * store_scope      : str | None -- 'tomball' | 'copperfield' | 'both' | NULL.
                              NULL (None) == all stores. 'both' is the explicit
                              both-stores token; we treat NULL and 'both' as
                              equivalent "all stores" for the corporate guard
                              and NORMALIZE to NULL.
  * email            : str | None -- the login identity a partner is pinned by.

These functions accept either a real User row (duck-typed via getattr) or a
plain str email / dict-like, so they can be unit-tested with no DB.

------------------------------------------------------------------------------
THE ESCAPE HATCH  (ROSTER_PARTNER_EMAILS)
------------------------------------------------------------------------------
The fixed allow-list is config-driven, not data-driven. By default it is the
two module constants below (SAM_EMAIL + MASOOD_EMAIL). To override the entire
allow-list at deploy time -- so an operator can never get locked out of the
partner tier if the pins are wrong / stale -- set the env var:

    ROSTER_PARTNER_EMAILS="a@x.com,b@y.com"   (comma-separated, case-insensitive)

When ROSTER_PARTNER_EMAILS is set and non-empty, it REPLACES the built-in
list entirely (it is the documented escape hatch / source of truth). When it
is unset, the built-in SAM_EMAIL + MASOOD_EMAIL (minus any None) is used.

The env var is read at CALL time (via partner_identities()), never cached at
import, so a test or a deploy can change it without re-importing the module.

------------------------------------------------------------------------------
MASOOD PLACEHOLDER + SAFE MODE
------------------------------------------------------------------------------
Masood's login email is not yet known, so MASOOD_EMAIL is a clearly-marked
placeholder (None). While Masood is UNPINNED (MASOOD_EMAIL is None AND no env
override supplies a second identity), the module runs in SAFE MODE:

  * The no-3rd-partner rule IS still hard-enforced: only emails in the
    resolved allow-list may be / become partner; everyone else is rejected.
  * The known pinned identities (Sam, and Masood once pinned) ARE still
    protected from demotion / removal.
  * BUT removing / demoting the as-yet-UNPINNED second slot is NOT
    hard-blocked -- there is no concrete identity to protect there, and we
    must not lock anyone out of fixing the tier before Masood is known.

Once Sam provides Masood's email (set MASOOD_EMAIL or ROSTER_PARTNER_EMAILS),
safe mode ends automatically and the second partner becomes fully protected.
"""
from __future__ import annotations

import os
from typing import Iterable

# ============================================================
# Fixed partner allow-list (config, not data).
# ============================================================
# Sam -- known login email (the operator). Pinned.
SAM_EMAIL: str = "samsahragard@gmail.com"

# Masood -- login email NOT yet known. Clearly-marked placeholder.
# TODO: Sam to provide Masood's login email/id. Until then the second partner
# slot is UNPINNED and the module runs in SAFE MODE (see module docstring).
MASOOD_EMAIL: str | None = None  # TODO: Sam to provide Masood's login email/id.

# The env var that is the documented ESCAPE HATCH: when set (non-empty), it
# REPLACES the built-in allow-list entirely so nobody can be locked out of the
# partner tier by a stale / wrong pin.
PARTNER_EMAILS_ENV = "ROSTER_PARTNER_EMAILS"

# The two permission_level values this module guards.
PARTNER_LEVEL = "partner"
CORPORATE_LEVEL = "corporate"

# store_scope tokens treated as "all stores" (both). NULL is canonical; 'both'
# is the explicit token. Both normalize to None (NULL).
_ALL_STORES_TOKENS = frozenset({"", "both"})


class TierInvariantError(Exception):
    """Raised when an action would violate a tier invariant (creating a 3rd
    partner, demoting a pinned partner, or a corporate user not scoped to both
    stores). Pure -- carries a human-readable message only."""


# ============================================================
# Identity helpers (pure; no DB, no Flask).
# ============================================================
def _norm_email(value) -> str | None:
    """Normalize an identity to a comparable email string: lowercased, trimmed.
    Accepts a plain str, or a User-like object with an .email attribute, or a
    dict with an 'email' key. Returns None when no email can be derived."""
    if value is None:
        return None
    if isinstance(value, str):
        email = value
    elif isinstance(value, dict):
        email = value.get("email")
    else:
        email = getattr(value, "email", None)
    if not email or not isinstance(email, str):
        return None
    email = email.strip().lower()
    return email or None


def _level_of(user) -> str | None:
    """The permission_level of a User-like object (or dict), lowercased.
    A plain str is treated as an email with no known level -> None."""
    if user is None or isinstance(user, str):
        return None
    if isinstance(user, dict):
        level = user.get("permission_level")
    else:
        level = getattr(user, "permission_level", None)
    if not level or not isinstance(level, str):
        return None
    return level.strip().lower()


def _env_partner_emails() -> list[str] | None:
    """The escape-hatch override list from ROSTER_PARTNER_EMAILS, normalized.
    Returns a list of lowercased emails when the env var is set and yields at
    least one non-blank entry, else None (no override -> use the built-ins).
    Read at CALL time so deploys / tests can change it without re-import."""
    raw = os.getenv(PARTNER_EMAILS_ENV)
    if not raw:
        return None
    emails = [e.strip().lower() for e in raw.split(",") if e.strip()]
    return emails or None


def partner_identities() -> frozenset[str]:
    """The RESOLVED fixed partner allow-list (set of lowercased emails).

    Resolution order (config-driven):
      1. ROSTER_PARTNER_EMAILS env var, if set+non-empty  -> the escape hatch,
         REPLACES the built-in list entirely.
      2. else the built-in SAM_EMAIL + MASOOD_EMAIL, with any None dropped
         (so an unpinned Masood simply contributes nothing -> a 1-entry list).

    Never raises; never reads the DB. The returned set can be empty only if
    every source is empty (e.g. SAM_EMAIL blanked AND no env) -- callers must
    not assume it is non-empty, though in practice SAM_EMAIL is always pinned.
    """
    override = _env_partner_emails()
    if override is not None:
        return frozenset(override)
    builtin = [e for e in (_norm_email(SAM_EMAIL), _norm_email(MASOOD_EMAIL))
               if e is not None]
    return frozenset(builtin)


def is_safe_mode() -> bool:
    """True when the partner allow-list has fewer than two pinned identities
    (i.e. Masood is not yet known and no env override supplies a second slot).

    In safe mode the no-3rd-partner rule + protection of the KNOWN pins still
    hold, but removal/demotion of the as-yet-unpinned second slot is NOT
    hard-blocked (there is no concrete identity to protect). See module
    docstring. Pure: derived from partner_identities()."""
    return len(partner_identities()) < 2


# ============================================================
# Partner guards.
# ============================================================
def can_be_partner(identity) -> bool:
    """Whether `identity` (a User-like obj, dict, or email str) is permitted to
    hold the partner tier -- i.e. its email is in the resolved fixed allow-list
    (case-insensitive). An identity with no derivable email can never be a
    partner. Pure; no DB."""
    email = _norm_email(identity)
    if email is None:
        return False
    return email in partner_identities()


def is_pinned_partner(identity) -> bool:
    """Whether `identity` is one of the CURRENTLY-PINNED partners (same as
    can_be_partner today). Named separately because the protection semantics
    (cannot be demoted/removed) read more clearly against 'pinned'."""
    return can_be_partner(identity)


def assert_partner_change_allowed(actor, target, action: str) -> None:
    """Guard a change to the partner tier. Raises TierInvariantError on a
    violation; returns None when the action is allowed.

    Parameters
    ----------
    actor   : the User-like identity performing the change (currently advisory
              -- recorded in the error message; no actor-side restriction is
              imposed here beyond the structural invariants, since Sam's
              decision is about the TARGET tier shape, not who may edit).
    target  : the User-like identity / email being created, promoted, demoted,
              or removed.
    action  : one of:
                'create'  / 'promote'   -> target is BECOMING a partner.
                'demote'  / 'remove' / 'delete' / 'deactivate'
                                        -> target is CEASING to be a partner.
              (case-insensitive; unknown actions are treated conservatively as
              a no-op pass -- this guard only knows about the partner tier
              transitions above.)

    Invariants enforced:
      * NO 3RD PARTNER: a create/promote whose target email is NOT in the
        resolved allow-list is rejected -- always, including safe mode.
      * NO DEMOTING A PINNED PARTNER: a demote/remove whose target IS a pinned
        partner is rejected -- always, including safe mode (the KNOWN pins are
        protected even before Masood is filled in).
      * SAFE MODE: when Masood is unpinned (allow-list < 2), a demote/remove of
        a target that is NOT a pinned partner is ALLOWED -- we do not block
        touching the empty second slot, so nobody is locked out while the pin
        is pending.
    """
    act = (action or "").strip().lower()
    target_email = _norm_email(target)
    actor_email = _norm_email(actor)

    if act in ("create", "promote"):
        # Becoming a partner -> must be on the allow-list. Rejects the 3rd.
        if not can_be_partner(target):
            raise TierInvariantError(
                f"refusing to make a 3rd partner: {target_email or '<no-email>'} "
                f"is not in the fixed partner allow-list "
                f"({sorted(partner_identities())}). Partner is exactly the two "
                f"fixed identities (Sam + Masood); use the {PARTNER_EMAILS_ENV} "
                f"escape hatch to change the allow-list. "
                f"(actor={actor_email or '<no-email>'}, action={act})"
            )
        return None

    if act in ("demote", "remove", "delete", "deactivate"):
        # Ceasing to be a partner -> a PINNED partner is protected.
        if is_pinned_partner(target):
            raise TierInvariantError(
                f"refusing to {act} a pinned partner: "
                f"{target_email or '<no-email>'} is one of the fixed partners. "
                f"Partner is exactly the two fixed identities and cannot be "
                f"demoted/removed; change the {PARTNER_EMAILS_ENV} escape hatch "
                f"allow-list instead. (actor={actor_email or '<no-email>'})"
            )
        # SAFE MODE: the unpinned second slot is NOT a pinned partner, so a
        # remove/demote of a non-pinned target passes (don't lock anyone out
        # before Masood is known). Same result outside safe mode -- a
        # non-partner being 'removed' from the partner tier is a no-op anyway.
        return None

    # Unknown / non-partner-tier action: nothing for this guard to enforce.
    return None


# ============================================================
# Corporate guard.
# ============================================================
def normalize_store_scope(scope) -> str | None:
    """Normalize a store_scope value to the canonical form used by the corporate
    guard. NULL (None), '' and 'both' all collapse to None (== all stores);
    'tomball'/'copperfield' (or any other concrete token) are lowercased and
    returned as-is. Pure; never raises."""
    if scope is None:
        return None
    s = str(scope).strip().lower()
    if s in _ALL_STORES_TOKENS:
        return None
    return s


def is_both_stores(scope) -> bool:
    """True when a store_scope means BOTH stores (NULL or 'both' or '')."""
    return normalize_store_scope(scope) is None


def assert_corporate_both_stores(user) -> None:
    """Guard the corporate invariant: a CORPORATE user must have store_scope
    NULL (both stores). Raises TierInvariantError when a corporate user is
    pinned to a single store; returns None otherwise.

    No-op for non-corporate users (only the corporate tier carries this
    invariant). Pure -- reads permission_level + store_scope off the User-like
    object; no DB. Accepts a dict or User row."""
    level = _level_of(user)
    if level != CORPORATE_LEVEL:
        return None
    if isinstance(user, dict):
        scope = user.get("store_scope")
    else:
        scope = getattr(user, "store_scope", None)
    if not is_both_stores(scope):
        norm = normalize_store_scope(scope)
        raise TierInvariantError(
            f"corporate user must span BOTH stores (store_scope NULL); got "
            f"store_scope={norm!r}. Corporate is the top tier across both "
            f"stores -- set store_scope to NULL (or 'both')."
        )
    return None


def assert_tier_invariants(user, *, actor=None) -> None:
    """Convenience aggregate for a single user row: enforces the corporate
    both-stores invariant, and -- if the user claims the partner level --
    that the user is on the fixed allow-list (a stored 3rd partner is a
    violation). Raises TierInvariantError on the first violation.

    This is the shape a future endpoint-wiring wave can call on save. Pure;
    no DB. Partner-tier transitions (create/demote) are guarded separately by
    assert_partner_change_allowed -- this checks the STATE of one row."""
    assert_corporate_both_stores(user)
    if _level_of(user) == PARTNER_LEVEL and not can_be_partner(user):
        email = _norm_email(user)
        raise TierInvariantError(
            f"stored partner {email or '<no-email>'} is not in the fixed "
            f"allow-list ({sorted(partner_identities())}) -- only the two "
            f"fixed identities may hold the partner tier."
        )
    return None


def all_identities_allowed(identities: Iterable) -> bool:
    """Convenience: True iff EVERY given identity that claims partner can be a
    partner. Helper for a future bulk validate; pure."""
    return all(can_be_partner(i) for i in identities)
