"""Schedules V2 - Block 4: manager DRAFT schedule-creation endpoints.

These routes ride the EXISTING store_bp blueprint (URL prefix /<store_slug>/),
so they inherit store_bp's gates for free:
  - _pull_store sets g.current_store (404 on an unknown store slug)
  - _per_store_gate enforces the audience gate BEFORE the view runs: a gm scoped
    to Tomball hitting /<copperfield>/... gets 403/redirect with ZERO rows touched
    (403-before-mutation). partner/corporate are unrestricted.
On top of that, @require_level("foh_manager") gates to MANAGERS (gm/manager/km/
assistant_km/corporate_chef/prep_manager/foh_manager + partner/corporate);
expo/driver get 403 and employees (SMS sessions, no keypad user) get redirected
to login -> they cannot create or read drafts here.

aick owns this file; ck builds the week-view UI against these endpoints. B7/B8
hooks (scheduling_timeoff.conflict / scheduling_availability.warning) are called
on shift create/update as no-op stubs now (ckai fills them in B7/B8).
"""
from __future__ import annotations

from datetime import date as _date, datetime

from flask import g, jsonify, request

from app.db import SessionLocal
from app.models import (
    CANONICAL_POSITIONS,
    Employee,
    EmployeePosition,
    EmployeeStoreAssignment,
    Position,
    Schedule,
    Shift,
    ShiftTag,
    Tag,
    User,
)
from app.services import scheduling_alarms, scheduling_availability, scheduling_timeoff
from app.web.permissions import current_user_id, require_level
from app.web.store_routes import store_bp

_MGR = "foh_manager"  # lowest manager level allowed to manage schedules

# Normalized lookup for CANONICAL_POSITIONS (Sam #2227): case/space-insensitive
# match so the board's position dropdown shows ONLY the 14 canonical jobs (13
# FOH + Cook) and hides Sling-import junk. Read-side filter only - never deletes
# a Position row (EmployeePosition.position_id is ondelete=CASCADE).
_CANONICAL_NORM = {name.lower(): name for name in CANONICAL_POSITIONS}


def _is_canonical_position(name: "str | None") -> bool:
    return (name or "").strip().lower() in _CANONICAL_NORM


def _store() -> str | None:
    """The store_key stored on schedules/shifts is the LOCATION
    ('tomball'/'copperfield'), NOT the URL slug ('dos'/'uno') - so it joins with
    employee_store_assignments.store_key + User.store_scope, which are
    location-keyed (B2 contract + B3 migration). The audience GATE still keys off
    the slug via store_bp._per_store_gate; only storage/filtering uses location.
    (ckai #1887 cross-block fix.)"""
    return getattr(g, "current_location", None)


def _dt(s):
    return datetime.fromisoformat(s) if s else None


# ==========================================================================
# S5 -- PLACEMENT drives PERMISSION (Sam team-roster store-split).
# When a manager adds/assigns someone into a (store, section), the SAME action
# that writes EmployeePosition(store_key)+EmployeeStoreAssignment also pushes the
# derived permission onto the linked User: permission_level = the highest-rank
# section role the person holds, store_scope = derived from their store-assignment
# set. This keeps PLACEMENT (the roster) and PERMISSION (require_level / the
# permission catalog) from drifting -- one write, one source of truth.
#
# These helpers are PURE over (db, employee) -- no Flask request state -- so the
# new test exercises them directly without standing up create_app. They are
# imported by schedules_v2_roster.py (which already pulls _MGR/_store from here).
# ==========================================================================
def _section_of_position_name(name):
    """The per-store SECTION ('management'|'hourly'|'driver'|None) for a canonical
    position NAME, via role_buckets. None = tier-above (partner/corporate),
    Expo/driver-access (no scheduling role), or unknown. Never raises."""
    from app.services.role_buckets import section_for_position
    return section_for_position(name)


def _highest_section_role(position_names):
    """The permission_catalog ROLE key with the HIGHEST ROLE_RANK among the given
    canonical position names that map to a SECTION (management/hourly/driver).

    Positions whose role is tier-above (partner/corporate -> section None) or that
    have no scheduling role (Expo, unknown) are ignored -- they never become the
    derived permission_level here (partner/corporate are set through the Team
    admin path + guarded separately). Ties break on the role key for determinism.
    Returns the role key (str) or None when no position maps to a section."""
    from app.services.permission_catalog import ROLE_RANK, position_role
    from app.services.role_buckets import section_for_position
    best_role = None
    best_rank = None
    for nm in position_names:
        if section_for_position(nm) is None:
            continue  # tier-above / no-section position -> never the derived level
        role = position_role(nm)
        if not role:
            continue
        rank = ROLE_RANK.get(role)
        if rank is None:
            continue
        if best_rank is None or rank > best_rank or (rank == best_rank and role < best_role):
            best_role, best_rank = role, rank
    return best_role


def _derive_store_scope(store_keys, level):
    """Derive User.store_scope from an employee's EmployeeStoreAssignment set.

    Convention (matches team_routes._parse_role_form + the User model):
      * partner / corporate -> NULL (they span every store implicitly).
      * single store         -> that store key ('tomball' | 'copperfield').
      * multiple stores       -> the sorted CSV ('copperfield,tomball').
      * no stores             -> NULL (nothing to scope to).
    Only the two canonical stores are considered; unknown keys are dropped."""
    if level in ("partner", "corporate"):
        return None
    keys = sorted({(s or "").strip().lower() for s in store_keys
                   if (s or "").strip().lower() in ("tomball", "copperfield")})
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    return ",".join(keys)


def _employee_position_names(db, emp_id):
    """The canonical position NAMES an employee currently holds (across stores),
    de-duped. Reads EmployeePosition -> Position.name; non-canonical (Sling junk)
    rows are dropped so they never drive the derived permission level."""
    rows = db.query(EmployeePosition).filter_by(employee_id=emp_id).all()
    pids = {r.position_id for r in rows}
    if not pids:
        return []
    names = []
    for p in db.query(Position).filter(Position.id.in_(pids)).all():
        if _is_canonical_position(p.name):
            names.append(p.name)
    return names


def _employee_store_keys(db, emp_id):
    """The set of store keys an employee is assigned to (the scope basis)."""
    return {a.store_key for a in
            db.query(EmployeeStoreAssignment).filter_by(employee_id=emp_id).all()}


def apply_section_placement_to_user(db, emp, actor):
    """Push the employee's DERIVED placement onto their linked User (if any).

    Reads the employee's current canonical positions + store assignments (the
    rows the add/assign endpoint just wrote), derives permission_level (highest
    section role) + store_scope, runs the tier GUARDS, and writes both onto the
    linked User. No-op when:
      * the employee has no linked User (invite/setup pending) -- the role is
        carried by the EmployeePosition rows themselves and applied to the User
        when it is created/linked (backfill_manager_positions /
        backfill_user_links at boot); we do NOT invent a parallel store here.
      * no position maps to a section (nothing to derive).

    GUARDS (tier_invariants): a derived partner is gated through
    assert_partner_change_allowed(create/promote); a derived corporate must span
    both stores (assert_corporate_both_stores). A TierInvariantError propagates to
    the caller, which turns it into a clean 4xx (NOT a 500). Does NOT commit --
    the caller owns the transaction. Returns the derived (level, store_scope) or
    (None, None) when nothing was applied."""
    if emp is None or getattr(emp, "user_id", None) is None:
        return (None, None)
    names = _employee_position_names(db, emp.id)
    level = _highest_section_role(names)
    if level is None:
        return (None, None)
    scope = _derive_store_scope(_employee_store_keys(db, emp.id), level)

    from app.services import tier_invariants as ti
    user = db.get(User, emp.user_id)
    if user is None:
        return (None, None)
    # GUARD partner: only the two pinned identities may BECOME a partner. We pass
    # a target carrying the candidate level + the user's email so the allow-list
    # check (keyed by email) runs; promoting to partner from a lower level is a
    # 'promote'.
    if level == "partner" and (user.permission_level or "").strip().lower() != "partner":
        ti.assert_partner_change_allowed(actor, user, "promote")
    # GUARD corporate: a corporate user must span both stores (store_scope NULL).
    if level == "corporate":
        ti.assert_corporate_both_stores({"permission_level": "corporate", "store_scope": scope})
    user.permission_level = level
    user.store_scope = scope
    return (level, scope)


@store_bp.route("/schedules-v2/employees/add", methods=["POST"])
@require_level(_MGR)
def sv2_employee_add():
    """Unify +Add (Sam #2261 / #2310 / #2312 / #2315): a manager adds a team
    member with NAME + PHONE + EMAIL + POSITION(s) + STORE(s). Creates the
    Employee (active; no passcode yet), assigns the store(s) + position(s) the
    manager picked, and fires the email setup invite
    (employee_setup.send_setup_invite -> a one-time /employee/setup/<token> link
    via orders@ SMTP); the hire then sets their 5-digit passcode + finishes their
    profile at that link.

    The manager sets store(s)/position(s) at add-time - a MANAGER action
    (require_level _MGR), NOT an employee self-assigning, so samai's #2097
    privilege boundary (an employee can't self-grant scheduling reach) still
    holds. POSITION + STORE are BOTH multi-select (Sam #2315): multi-position
    (EmployeePosition M2M) + multi-store (one EmployeeStoreAssignment per store =
    the schedulability gate, so the hire is immediately schedulable at each).
    Email is the login IDENTITY; duplicate email (and phone) are rejected."""
    data = request.get_json(silent=True) or {}
    full_name = (data.get("full_name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip() or None

    def _as_list(v):
        if v is None:
            return []
        return v if isinstance(v, list) else [v]
    position_ids = []
    for p in _as_list(data.get("position_ids")):
        try:
            position_ids.append(int(p))
        except (TypeError, ValueError):
            pass
    store_keys = [str(s).strip().lower() for s in _as_list(data.get("store_keys")) if str(s).strip()]

    # Per-store position assignment (Sam #2435/#2457): the FE sends explicit
    # (position, store) pairs as `assignments` -- one person can be Manager @
    # Tomball + Server @ Copperfield on one login. Back-compat: with no
    # `assignments`, the old position_ids[] x store_keys[] is the cartesian
    # (every picked position at every picked store). Either way EmployeePosition
    # now carries store_key.
    pairs = []
    for a in _as_list(data.get("assignments")):
        if not isinstance(a, dict):
            continue
        try:
            apid = int(a.get("position_id"))
        except (TypeError, ValueError):
            continue
        ask = str(a.get("store_key") or "").strip().lower()
        if ask:
            pairs.append((apid, ask))
    if pairs:
        position_ids = list(dict.fromkeys([p for p, _ in pairs]))
        store_keys = list(dict.fromkeys([s for _, s in pairs] + store_keys))

    if not full_name:
        return jsonify({"ok": False, "error": "Name is required."}), 400
    parts = email.split("@")
    if len(parts) != 2 or not parts[0] or "." not in parts[1] or parts[1].endswith("."):
        return jsonify({"ok": False, "error": "A valid email is required."}), 400
    bad_stores = [s for s in store_keys if s not in ("tomball", "copperfield")]
    if bad_stores:
        return jsonify({"ok": False, "error": "Unknown store(s): %s." % ", ".join(bad_stores)}), 400
    if not store_keys:
        return jsonify({"ok": False, "error": "Pick at least one store so they are schedulable."}), 400

    # S5 SECTION-SCOPED add: the FE may send an explicit 'section'
    # ('management'|'hourly'|'driver'); if absent we DERIVE it from the chosen
    # positions below. A mixed add (management + hourly positions in one call) is
    # rejected -- one +Add is one section (Sam team-roster split). Normalized here;
    # validated against the positions inside the DB block (needs the names).
    req_section = (data.get("section") or "").strip().lower() or None
    if req_section is not None and req_section not in ("management", "hourly", "driver"):
        return jsonify({"ok": False, "error": "Unknown section: %s." % req_section}), 400

    db = SessionLocal()
    try:
        # Email is the login identity (login + invite key off it), so it must be
        # unique. Phone is the SMS-identity UNIQUE. Guard both here (case-insensitive
        # email; small-table scan).
        all_emps = db.query(Employee).all()
        if any((e.email or "").lower() == email.lower() for e in all_emps):
            return jsonify({"ok": False,
                            "error": "An employee with that email already exists."}), 409
        if phone and any((e.phone or "") == phone for e in all_emps):
            return jsonify({"ok": False,
                            "error": "An employee with that phone already exists."}), 409
        # Validate position_ids against the CANONICAL catalog (the 14) - a manager
        # can only assign real positions (mirrors the cleaned dropdown).
        valid_pids = set()
        if position_ids:
            _canon_lc = {c.lower() for c in CANONICAL_POSITIONS}
            for p in db.query(Position).filter(Position.id.in_(position_ids)).all():
                if (p.name or "").strip().lower() in _canon_lc:
                    valid_pids.add(p.id)
            if [pid for pid in position_ids if pid not in valid_pids]:
                return jsonify({"ok": False, "error": "Unknown or non-canonical position(s)."}), 400

        # The canonical names of the validated positions (used by both the
        # section-consistency check + the rank-gate below).
        _valid_names = [p.name for p in
                        db.query(Position).filter(Position.id.in_(valid_pids)).all()]

        # S5 SECTION-SCOPED: every chosen position must belong to the SAME section
        # (no mixing management + hourly in one +Add). Positions with a real
        # section (management/hourly/driver) are checked; tier-above / no-section
        # positions (partner/corporate/Expo) carry no section and are skipped here
        # (partner/corporate flow through Team admin + the tier guards). If the FE
        # sent an explicit 'section' it must match; else we derive it.
        from app.services.role_buckets import section_for_position
        _pos_sections = {section_for_position(nm) for nm in _valid_names}
        _pos_sections.discard(None)
        if len(_pos_sections) > 1:
            return jsonify({"ok": False,
                            "error": "One +Add is one section - don't mix %s."
                                     % " + ".join(sorted(_pos_sections))}), 400
        derived_section = next(iter(_pos_sections), None)
        if req_section is not None and derived_section is not None and req_section != derived_section:
            return jsonify({"ok": False,
                            "error": "Positions are %s, not %s." % (derived_section, req_section)}), 400
        section = req_section or derived_section

        # Rank-gate (Sam #2381 / #2404 + S5 section-tightening): a manager adds
        # only roles STRICTLY BELOW their own rank AND within the chosen section
        # tier. addable_roles() already encodes the strictly-below-rank rule; we
        # additionally require each chosen position's role to sit in `section`
        # (via role_buckets) so a manager can't slip a same-section higher tier in.
        # Authoritative server check (the +Add dropdown is rank+section-filtered
        # FE-side too). partner + corporate are the only tiers that add GM/KM/
        # Corp-Chef.
        from app.services.permission_catalog import addable_roles, position_role
        _allowed = addable_roles(getattr(db.get(User, current_user_id()), "permission_level", None))
        _over = sorted({nm for nm in _valid_names
                        if position_role(nm) and position_role(nm) not in _allowed})
        if _over:
            return jsonify({"ok": False,
                            "error": "Your role can only add positions below your own - not: %s."
                                     % ", ".join(_over)}), 403
        # Section-tier mismatch: a chosen position whose section differs from the
        # add's section (only possible when the FE forced a section that some
        # position contradicts -- the consistency check above already blocks the
        # cross-section mix, so this is the belt for a single-position forced add).
        if section is not None:
            _wrong = sorted({nm for nm in _valid_names
                             if section_for_position(nm) not in (None, section)})
            if _wrong:
                return jsonify({"ok": False,
                                "error": "Positions outside the %s section: %s."
                                         % (section, ", ".join(_wrong))}), 400

        emp = Employee(full_name=full_name, email=email, phone=phone, active=True)
        db.add(emp)
        # S5 PLACEMENT->PERMISSION: a brand-new +Add employee has NO linked User
        # yet (the User is created later via Team admin and linked, or the boot
        # backfill links it). So apply_section_placement_to_user is a NO-OP here
        # (user_id is None) -- the derived role is carried by the EmployeePosition
        # rows we write below, and backfill_manager_positions / backfill_user_links
        # set the User's permission_level from those rows when the User appears. We
        # still run the tier GUARD on the DERIVED level so a management +Add that
        # would imply a partner/corporate placement is rejected up front (clean
        # 4xx, never a 500) rather than silently creating an unguarded row.
        from app.services import tier_invariants as ti
        try:
            db.flush()
            for sk in dict.fromkeys(store_keys):       # multi-store (Sam #2315), de-duped
                db.add(EmployeeStoreAssignment(employee_id=emp.id, store_key=sk))
            # EmployeePosition now carries store_key (per-store positions, Sam
            # #2435). Write the explicit valid (position, store) pairs; else (old
            # cartesian path) every valid position at every picked store.
            _ep_pairs = ([(p, s) for (p, s) in pairs if p in valid_pids] if pairs
                         else [(p, s) for p in valid_pids for s in store_keys])
            for (pid, sk) in dict.fromkeys(_ep_pairs):
                db.add(EmployeePosition(employee_id=emp.id, position_id=pid, store_key=sk))
            # Derive the placement permission from the just-written positions +
            # stores, run the tier guards, and -- if a User is somehow already
            # linked (re-add of an existing identity) -- write it onto the User.
            derived_level = _highest_section_role(_valid_names)
            if derived_level == "partner":
                ti.assert_partner_change_allowed(
                    db.get(User, current_user_id()), {"email": email}, "create")
            if derived_level == "corporate":
                ti.assert_corporate_both_stores(
                    {"permission_level": "corporate",
                     "store_scope": _derive_store_scope(store_keys, derived_level)})
            apply_section_placement_to_user(
                db, emp, db.get(User, current_user_id()))
            db.commit()
        except ti.TierInvariantError as e:
            db.rollback()
            return jsonify({"ok": False, "error": str(e)}), 409
        except Exception:
            db.rollback()
            return jsonify({"ok": False,
                            "error": "Could not add (duplicate phone or data conflict)."}), 409
        emp_id = emp.id
    finally:
        db.close()

    # Fire the invite AFTER our session closes (send_setup_invite opens its own).
    # It never raises into us (logs the link on SMTP failure), so the add still
    # succeeds + a re-add/re-invite just issues a fresh token.
    from app.web.employee_setup import send_setup_invite
    invite = send_setup_invite(emp_id)
    # Dual-channel (Sam 2026-06-07): the invite mints BOTH an emailed link and a
    # manager-displayed code on one single-use token. Surface the code so the
    # manager can hand it over; omit gracefully if there was no invite (no email).
    resp = {"ok": True, "employee_id": emp_id,
            "stores": store_keys, "position_ids": sorted(valid_pids),
            "message": "Invitation emailed to %s." % email}
    setup_code = (invite or {}).get("code")
    if setup_code:
        resp["setup_code"] = setup_code
    return jsonify(resp), 200


@store_bp.route("/schedules-v2/schedule/new", methods=["POST"])
@require_level(_MGR)
def sv2_schedule_new():
    data = request.get_json(silent=True) or {}
    ws = (data.get("week_start") or "").strip()
    try:
        week_start = _date.fromisoformat(ws)
    except Exception:
        return jsonify({"ok": False, "error": "week_start required (YYYY-MM-DD)"}), 400
    db = SessionLocal()
    try:
        existing = db.query(Schedule).filter_by(store_key=_store(), week_start=week_start).first()
        if existing:
            return jsonify({"ok": False, "error": "a schedule for that week already exists",
                            "id": existing.id}), 409
        now = datetime.utcnow()
        sched = Schedule(store_key=_store(), week_start=week_start, status="draft",
                         created_by=current_user_id(), created_at=now, updated_at=now)
        db.add(sched)
        db.commit()
        return jsonify({"ok": True, "id": sched.id, "store": _store(),
                        "week_start": week_start.isoformat(), "status": "draft"}), 201
    finally:
        db.close()


@store_bp.route("/schedules-v2/schedule", methods=["GET"])
@require_level(_MGR)
def sv2_schedule_list():
    db = SessionLocal()
    try:
        q = db.query(Schedule).filter_by(store_key=_store())
        wk = request.args.get("week")
        if wk:
            try:
                q = q.filter_by(week_start=_date.fromisoformat(wk))
            except Exception:
                pass
        out = [{"id": s.id, "week_start": s.week_start.isoformat(), "status": s.status,
                "published_at": s.published_at.isoformat() if s.published_at else None}
               for s in q.order_by(Schedule.week_start.desc()).all()]
        return jsonify({"ok": True, "store": _store(), "schedules": out}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/board", methods=["GET"])
@require_level(_MGR)
def sv2_board():
    """Single round-trip for the week-view grid (ck #1893): the week's schedule +
    its shifts + the store roster + positions + tags. No schedule for the week ->
    schedule:null + shifts:[] (UI shows a create-draft CTA)."""
    wk = (request.args.get("week") or "").strip()
    try:
        week_start = _date.fromisoformat(wk)
    except Exception:
        return jsonify({"ok": False, "error": "week required (YYYY-MM-DD)"}), 400
    store = _store()  # location ("tomball"/"copperfield")
    db = SessionLocal()
    try:
        sched = db.query(Schedule).filter_by(store_key=store, week_start=week_start).first()
        shifts = []
        if sched:
            for sh in db.query(Shift).filter_by(schedule_id=sched.id).all():
                tag_ids = [st.tag_id for st in db.query(ShiftTag).filter_by(shift_id=sh.id).all()]
                shifts.append({
                    "id": sh.id, "employee_id": sh.employee_id, "position_id": sh.position_id,
                    "start_at": sh.start_at.isoformat() if sh.start_at else None,
                    "end_at": sh.end_at.isoformat() if sh.end_at else None,
                    "break_minutes": sh.break_minutes, "status": sh.status,
                    "notes": sh.notes, "tag_ids": tag_ids,
                    "display_name": sh.display_name,  # Sam #2872: former-staff name (employee_id NULL) -> struck-through
                    "published_at": sh.published_at.isoformat() if sh.published_at else None,  # per-shift publish: null = hollow/unpublished
                })
        # positions: filtered to the CANONICAL 14 (Sam #2227 - 13 FOH + Cook);
        # Sling-import junk (C-Grill, C-Prep, Chba, Dish, ...) is hidden. store_key
        # null = all-store. NON-DESTRUCTIVE read filter - never deletes a row
        # (EmployeePosition cascade). Missing canonical names are seeded at boot
        # (app/__init__.py), so the dropdown always offers the full 14.
        positions = [{"id": p.id, "name": p.name, "store_key": p.store_key}
                     for p in db.query(Position).order_by(Position.name).all()
                     if _is_canonical_position(p.name)]
        # position_ids each employee HOLDS at THIS store -> lets the week-view
        # position filter surface the people who can work a role (Sam #2589): pick
        # Busser and only Busser-holders stay schedulable. Keyed off
        # EmployeePosition.store_key (the location, == `store`); restricted to the
        # canonical ids above so junk positions never gate the roster. A NULL-store
        # EmployeePosition row (pre per-store backfill) is treated as held at every
        # store the employee is rostered to, so an un-backfilled holder isn't hidden.
        _canon_pids = {p["id"] for p in positions}
        pos_by_emp: dict[int, set[int]] = {}
        for ep in db.query(EmployeePosition).all():
            if ep.position_id not in _canon_pids:
                continue
            sk = (ep.store_key or "").strip().lower()
            if sk and sk != store:
                continue  # held at the OTHER store only
            pos_by_emp.setdefault(ep.employee_id, set()).add(ep.position_id)
        # roster = employees assigned to THIS store (location-keyed), each carrying
        # the canonical position ids they hold here (position_ids, additive).
        emp_ids = [a.employee_id for a in
                   db.query(EmployeeStoreAssignment).filter_by(store_key=store).all()]
        roster = []
        if emp_ids:
            for e in (db.query(Employee).filter(Employee.id.in_(emp_ids))
                        .order_by(Employee.full_name).all()):
                roster.append({"id": e.id, "full_name": e.full_name, "active": e.active,
                               "position_ids": sorted(pos_by_emp.get(e.id, set()))})
        tags = [{"id": t.id, "name": t.name} for t in db.query(Tag).order_by(Tag.name).all()]
        return jsonify({
            "ok": True, "store": store,
            "schedule": ({"id": sched.id, "week_start": sched.week_start.isoformat(),
                          "status": sched.status,
                          "published_at": sched.published_at.isoformat() if sched.published_at else None}
                         if sched else None),
            "shifts": shifts, "roster": roster, "positions": positions, "tags": tags,
        }), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/shifts/new", methods=["POST"])
@require_level(_MGR)
def sv2_shift_new():
    data = request.get_json(silent=True) or {}
    sid = data.get("schedule_id")
    start_at, end_at = _dt(data.get("start_at")), _dt(data.get("end_at"))
    if not sid or not start_at or not end_at:
        return jsonify({"ok": False, "error": "schedule_id, start_at, end_at required"}), 400
    db = SessionLocal()
    try:
        # schedule must belong to THIS store (no cross-store shift insertion)
        sched = db.query(Schedule).filter_by(id=sid, store_key=_store()).first()
        if sched is None:
            return jsonify({"ok": False, "error": "schedule not found in this store"}), 404
        emp_id = data.get("employee_id")
        status = "assigned" if emp_id else "open"
        if emp_id:
            blocker = scheduling_timeoff.conflict(emp_id, start_at.date())  # B7 hook
            if blocker:
                return jsonify({"ok": False, "error": blocker}), 409
        warn = scheduling_availability.warning(emp_id, start_at) if emp_id else None  # B8 hook
        now = datetime.utcnow()
        sh = Shift(schedule_id=sid, employee_id=emp_id, position_id=data.get("position_id"),
                   start_at=start_at, end_at=end_at, break_minutes=int(data.get("break_minutes") or 0),
                   status=status, notes=data.get("notes"), created_at=now, updated_at=now)
        db.add(sh)
        db.flush()
        for tid in (data.get("tag_ids") or []):
            db.add(ShiftTag(shift_id=sh.id, tag_id=tid, created_at=now))
        db.commit()
        return jsonify({"ok": True, "id": sh.id, "status": status, "warning": warn}), 201
    finally:
        db.close()


def _shift_in_store(db, shift_id):
    """A shift whose schedule belongs to the current store, else None."""
    return (db.query(Shift)
              .join(Schedule, Shift.schedule_id == Schedule.id)
              .filter(Shift.id == shift_id, Schedule.store_key == _store())
              .first())


@store_bp.route("/schedules-v2/shifts/<int:shift_id>", methods=["PUT"])
@require_level(_MGR)
def sv2_shift_update(shift_id):
    data = request.get_json(silent=True) or {}
    db = SessionLocal()
    try:
        sh = _shift_in_store(db, shift_id)
        if sh is None:
            return jsonify({"ok": False, "error": "shift not found in this store"}), 404
        if "start_at" in data:
            sh.start_at = _dt(data["start_at"])
        if "end_at" in data:
            sh.end_at = _dt(data["end_at"])
        if "position_id" in data:
            sh.position_id = data["position_id"]
        if "break_minutes" in data:
            sh.break_minutes = int(data["break_minutes"] or 0)
        if "notes" in data:
            sh.notes = data["notes"]
        if "employee_id" in data:
            sh.employee_id = data["employee_id"]
            sh.status = "assigned" if data["employee_id"] else "open"
        # B7: an update that leaves the shift ASSIGNED must respect approved
        # time-off on the (possibly new) employee + (possibly new) date - the same
        # guard sv2_shift_new applies on create. Nothing is committed yet, so a 409
        # here leaves the shift untouched (ckai flagged this PUT-reassign gap #1974).
        if sh.employee_id and sh.start_at:
            blocker = scheduling_timeoff.conflict(sh.employee_id, sh.start_at.date())
            if blocker:
                return jsonify({"ok": False, "error": blocker}), 409
        # B8: availability is a SOFT advisory - surface it in the response, never
        # block (mirrors sv2_shift_new). ckai filled warning() in B8; I wire the PUT
        # call-site here per his recommendation, so create + PUT both return
        # "warning". (bulk-copy intentionally skipped: a soft per-shift advisory
        # doesn't fit a bulk op and the employee CAN still work - ckai #1984.)
        warn = (scheduling_availability.warning(sh.employee_id, sh.start_at)
                if (sh.employee_id and sh.start_at) else None)
        sh.updated_at = datetime.utcnow()
        db.commit()
        return jsonify({"ok": True, "id": sh.id, "status": sh.status, "warning": warn}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/shifts/<int:shift_id>", methods=["DELETE"])
@require_level(_MGR)
def sv2_shift_delete(shift_id):
    db = SessionLocal()
    try:
        sh = _shift_in_store(db, shift_id)
        if sh is None:
            return jsonify({"ok": False, "error": "shift not found in this store"}), 404
        db.query(ShiftTag).filter_by(shift_id=sh.id).delete()
        db.delete(sh)
        db.commit()
        return jsonify({"ok": True, "deleted": shift_id}), 200
    finally:
        db.close()


@store_bp.route("/schedules-v2/shifts/bulk-copy", methods=["POST"])
@require_level(_MGR)
def sv2_shift_bulk_copy():
    """Copy every shift from a source schedule into a target schedule (same
    store), shifting each shift's datetimes by the week delta. Template-a-week."""
    data = request.get_json(silent=True) or {}
    src_id, dst_id = data.get("from_schedule_id"), data.get("to_schedule_id")
    if not src_id or not dst_id:
        return jsonify({"ok": False, "error": "from_schedule_id + to_schedule_id required"}), 400
    db = SessionLocal()
    try:
        src = db.query(Schedule).filter_by(id=src_id, store_key=_store()).first()
        dst = db.query(Schedule).filter_by(id=dst_id, store_key=_store()).first()
        if src is None or dst is None:
            return jsonify({"ok": False, "error": "source/target schedule not found in this store"}), 404
        offset = dst.week_start - src.week_start  # timedelta (week delta)
        now = datetime.utcnow()
        n = 0
        opened_for_timeoff = 0
        for sh in db.query(Shift).filter_by(schedule_id=src.id).all():
            new_start = sh.start_at + offset
            emp_id, status = sh.employee_id, sh.status
            # B7: don't template an assignment onto the employee's approved time-off.
            # Rather than 409 the whole copy for one conflict (bad UX), bring that
            # slot over as OPEN (coverage preserved) + report the count so the
            # manager can re-fill it. (ckai flagged this bulk-copy gap #1974.)
            if emp_id and scheduling_timeoff.conflict(emp_id, new_start.date()):
                emp_id, status = None, "open"
                opened_for_timeoff += 1
            db.add(Shift(schedule_id=dst.id, employee_id=emp_id, position_id=sh.position_id,
                         start_at=new_start, end_at=sh.end_at + offset,
                         break_minutes=sh.break_minutes, status=status, notes=sh.notes,
                         created_at=now, updated_at=now))
            n += 1
        db.commit()
        return jsonify({"ok": True, "copied": n, "to_schedule_id": dst_id,
                        "opened_for_timeoff": opened_for_timeoff}), 201
    finally:
        db.close()


@store_bp.route("/schedules-v2/schedule/<int:schedule_id>/publish", methods=["POST"])
@require_level(_MGR)
def sv2_schedule_publish(schedule_id):
    """B5: publish a draft -> its shifts become visible to the assigned employees
    (who can accept/decline). Idempotent (re-publish stays published). After the
    status flip, fire the B6 alarm hook. Manager-gated + store-scoped."""
    db = SessionLocal()
    try:
        sched = db.query(Schedule).filter_by(id=schedule_id, store_key=_store()).first()
        if sched is None:
            return jsonify({"ok": False, "error": "schedule not found in this store"}), 404
        now = datetime.utcnow()
        sched.status = "published"
        if sched.published_at is None:
            sched.published_at = now
        sched.updated_at = now
        # per-shift publish: stamp every still-unpublished shift in this week so publish
        # flips the pending/new/edited shifts hollow->filled + makes them visible to the
        # employee. Already-published shifts keep their original published_at.
        for _sh in db.query(Shift).filter_by(schedule_id=sched.id).filter(Shift.published_at.is_(None)).all():
            _sh.published_at = now
        db.commit()
        # B6 hook (ckai #1912): create pending shift_alarms AFTER the status flip.
        # No-op stub today; wrapped so a future B6 error can never fail the publish.
        try:
            scheduling_alarms.create_for_schedule(sched.id)
        except Exception:  # noqa: BLE001
            pass
        return jsonify({"ok": True, "id": sched.id, "status": sched.status,
                        "published_at": sched.published_at.isoformat()}), 200
    finally:
        db.close()
