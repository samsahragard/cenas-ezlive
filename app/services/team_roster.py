"""Unified Team roster read (Project 1, Sam #2261 Team+Schedule combine).

The data behind the Team sub-tab: every team member by store with the positions
they fill, the BOH/FOH split, and the 3 stat cards. Managers/partners appear via
the Employee.user_id link (their permission_level surfaced as access_role). Pure
read-logic, framework-free: ckai wraps the HTTP endpoint on top of team_roster();
ck's FE binds to the returned shape. The roster is store-based - an employee
appears under each store they're assigned to (multi-store), so a person on both
stores shows in both sections; the 'all' count is distinct employees.
"""
from __future__ import annotations

from app.models import (CANONICAL_POSITIONS, Employee, EmployeePosition,
                        EmployeeStoreAssignment, Position, Shift, User)
from app.services.role_hierarchy import role_domain

# Canonical Position NAME -> role_hierarchy role key, so role_domain (which keys
# off role/permission_level values) classifies each position BOH/FOH. 'Well'
# (the bar/service well) maps to bartender = FOH; 'Hostess' -> host.
_POSITION_ROLE_KEY = {
    "partner": "partner", "corporate": "corporate", "corporate chef": "corporate_chef",
    "gm": "gm", "km": "km", "assistant km": "assistant_km", "foh manager": "foh_manager",
    "busser": "busser", "hostess": "host", "cashier": "cashier", "server": "server",
    "well": "bartender", "bartender": "bartender", "cook": "cook",
}
_CANON_LC = {p.lower() for p in CANONICAL_POSITIONS}
_STORE_LABELS = {"tomball": "Tomball", "copperfield": "Copperfield"}
_STORE_ORDER = ["tomball", "copperfield"]


def _domains(pos_names):
    """BOH/FOH domain set for canonical position names. 'both' roles
    (partner/corporate/gm) expand to {'boh','foh'}; subset of {'boh','foh'}."""
    out = set()
    for n in pos_names:
        key = _POSITION_ROLE_KEY.get((n or "").strip().lower())
        if not key:
            continue
        d = role_domain(key)  # 'kitchen' | 'foh' | 'both'
        if d in ("kitchen", "both"):
            out.add("boh")
        if d in ("foh", "both"):
            out.add("foh")
    return out


def _domain_label(dom):
    if "boh" in dom and "foh" in dom:
        return "both"
    if "boh" in dom:
        return "boh"
    if "foh" in dom:
        return "foh"
    return ""


def team_roster(db, location="all", position="all", include_inactive=False, flt="all"):
    """Team-sub-tab roster shape. location='all'|store_key; position='all'|name;
    flt='all'|'boh'|'foh'. Returns {ok, filter, location, include_inactive,
    counts:{all,boh,foh}, stats:{showing,active_total,positions},
    stores:[{store_key,label,shown,active,employees:[{id,full_name,active,
    positions:[{id,name}],domain,access_role,phone,email}]}]}."""
    location = (location or "all").strip().lower()
    position = (position or "all").strip()
    flt = (flt or "all").strip().lower()

    # canonical positions by id (junk filtered out, same set as the dropdown)
    canon = {p.id: p.name for p in db.query(Position).all()
             if (p.name or "").strip().lower() in _CANON_LC}
    emp_pos = {}  # employee_id -> [(position_id, name)]
    for ep in db.query(EmployeePosition).all():
        nm = canon.get(ep.position_id)
        if nm:
            emp_pos.setdefault(ep.employee_id, []).append((ep.position_id, nm))
    emp_stores = {}  # employee_id -> {store_key}
    for a in db.query(EmployeeStoreAssignment).all():
        emp_stores.setdefault(a.employee_id, set()).add(a.store_key)
    employees = {e.id: e for e in db.query(Employee).all()}
    role_by_uid = {}
    uids = {e.user_id for e in employees.values() if e.user_id}
    if uids:
        for u in db.query(User).filter(User.id.in_(uids)).all():
            role_by_uid[u.id] = u.permission_level

    def _record(e):
        plist = sorted(emp_pos.get(e.id, []), key=lambda x: x[1])
        dom = _domains([nm for _pid, nm in plist])
        return {
            "id": e.id, "full_name": e.full_name, "active": bool(e.active),
            "positions": [{"id": pid, "name": nm} for pid, nm in plist],
            "domain": _domain_label(dom),
            "access_role": role_by_uid.get(e.user_id),
            "phone": e.phone, "email": e.email,
            "_dom": dom, "_stores": emp_stores.get(e.id, set()),
        }

    # candidates = employees with >=1 store assignment (roster is store-based),
    # honoring include_inactive.
    candidates = []
    for eid in emp_stores:
        e = employees.get(eid)
        if e is None or (not include_inactive and not e.active):
            continue
        candidates.append(_record(e))

    in_loc = [r for r in candidates
              if location == "all" or location in r["_stores"]]
    # pills: domain totals over the location scope (before flt/position)
    counts = {
        "all": len({r["id"] for r in in_loc}),
        "boh": len({r["id"] for r in in_loc if "boh" in r["_dom"]}),
        "foh": len({r["id"] for r in in_loc if "foh" in r["_dom"]}),
    }

    def _passes(r):
        if flt == "boh" and "boh" not in r["_dom"]:
            return False
        if flt == "foh" and "foh" not in r["_dom"]:
            return False
        if position != "all" and not any(
                (p["name"] or "").strip().lower() == position.lower()
                for p in r["positions"]):
            return False
        return True
    shown = [r for r in in_loc if _passes(r)]

    active_total = len({e.id for e in employees.values()
                        if e.active and e.id in emp_stores})
    stats = {
        "showing": len({r["id"] for r in shown}),
        "active_total": active_total,
        "positions": len({p["name"] for r in shown for p in r["positions"]}),
    }

    stores_out = []
    for sk in ([location] if location != "all" else _STORE_ORDER):
        members = sorted([r for r in shown if sk in r["_stores"]],
                         key=lambda r: (r["full_name"] or "").lower())
        clean = [{k: v for k, v in r.items() if not k.startswith("_")}
                 for r in members]
        stores_out.append({
            "store_key": sk,
            "label": _STORE_LABELS.get(sk, (sk or "").title()),
            "shown": len(clean),
            "active": sum(1 for r in clean if r["active"]),
            "employees": clean,
        })

    return {
        "ok": True, "filter": flt, "location": location,
        "include_inactive": bool(include_inactive),
        "counts": counts, "stats": stats, "stores": stores_out,
    }


def backfill_user_links(db):
    """Idempotent unify reconcile (Sam #2261, ckai seam #2295; dedup #2370-#2374).
    Link each ACTIVE User to its Employee, with a name fallback that prevents the
    email-only-match duplicate ckbro spotted (Adriana Herrera x2):
      1. exact email match to a single UNLINKED employee, else
      2. EXACTLY-ONE same-name UNLINKED employee (a manager already in the roster
         under a different/blank email), else
      3. CREATE + link + store-assign a new Employee (a genuine pure manager).
    Also CONSOLIDATES an earlier email-only-match dup: a User linked to a BARE
    created row (no positions, no phone, no shifts) while a real same-name employee
    exists -> move the link to the real row + drop the bare dup (no data loss).
    Name COLLISIONS (>1 same-name UNLINKED) are SKIPPED, never mislinked (ckai
    #2374). A created manager gets one EmployeeStoreAssignment per store in their
    User.store_scope (NULL/'both' -> both). The User row + keypad auth are
    untouched. Returns (linked, created). Safe at boot / re-run (re-run -> 0,0).
    """
    emps = list(db.query(Employee).all())
    pos_ids = {ep.employee_id for ep in db.query(EmployeePosition).all()}

    def norm(s):
        return (s or "").strip().lower()

    def has_data(e):
        # a "real" roster row (has positions or a phone) vs a bare created row
        return (e.id in pos_ids) or bool(norm(e.phone))

    def email_match(addr):
        addr = norm(addr)
        if not addr:
            return None
        hit = [e for e in emps if e.user_id is None and norm(e.email) == addr]
        return hit[0] if len(hit) == 1 else None

    def lone_name(name, exclude):
        name = norm(name)
        if not name:
            return None
        hit = [e for e in emps if e.user_id is None and e is not exclude
               and norm(e.full_name) == name]
        return hit[0] if len(hit) == 1 else None

    def has_shifts(e):
        return db.query(Shift.id).filter(Shift.employee_id == e.id).first() is not None

    linked = created = consolidated = 0
    for u in db.query(User).filter(User.active.is_(True)).all():
        current = next((e for e in emps if e.user_id == u.id), None)
        # already linked to a real roster row -> nothing to do
        if current is not None and has_data(current):
            continue
        # best UNLINKED target: exact email, else exactly-one same-name
        target = email_match(u.email) or lone_name(u.full_name, exclude=current)
        # CONSOLIDATE: current is a BARE row (failed has_data); if a real same-name
        # target exists and the dup carries no shifts, move the link + drop the dup
        if current is not None:
            if target is not None and has_data(target) and not has_shifts(current):
                db.query(EmployeePosition).filter_by(employee_id=current.id).delete()
                db.query(EmployeeStoreAssignment).filter_by(
                    employee_id=current.id).delete()
                db.delete(current)
                emps.remove(current)
                target.user_id = u.id
                consolidated += 1
                linked += 1
            continue  # no real target / ambiguous / has shifts -> leave the row
        # not linked yet -> link the target if found
        if target is not None:
            target.user_id = u.id
            linked += 1
            continue
        # no email + a same-name COLLISION blocked the match -> skip + FLAG (never
        # dup/mislink); surfaced so a human links the manager manually (ckai #2374)
        if norm(u.full_name) and len(
                [e for e in emps if e.user_id is None
                 and norm(e.full_name) == norm(u.full_name)]) > 1:
            import logging
            logging.getLogger(__name__).warning(
                "unify reconcile: same-name collision, manager %r not auto-linked "
                "(link manually)", u.full_name)
            continue
        # genuine pure manager (no existing employee) -> create + link + store-assign
        emp = Employee(full_name=u.full_name, email=u.email, phone=None, active=True)
        db.add(emp)
        db.flush()
        created += 1
        scope = norm(u.store_scope)
        stores = (["tomball", "copperfield"] if scope in ("", "both")
                  else [scope] if scope in ("tomball", "copperfield") else [])
        for sk in stores:
            db.add(EmployeeStoreAssignment(employee_id=emp.id, store_key=sk))
        emp.user_id = u.id
        emps.append(emp)
        linked += 1
    if linked or created or consolidated:
        db.commit()
    return linked, created


def addable_positions_for(actor_role, db):
    """Canonical Position rows [{id, name}] that an actor of `actor_role` may
    ADD - computed from the SAME addable_roles() + position_role() the +Add 403
    gate uses, so the +Add dropdown the FE renders can never drift from the
    enforcement (Sam #2381/#2404). Sorted by name. Unknown/None actor -> []."""
    from app.services.permission_catalog import addable_roles, position_role
    allowed = addable_roles(actor_role)
    if not allowed:
        return []
    out = []
    for p in db.query(Position).all():
        nm = (p.name or "").strip()
        if nm.lower() in _CANON_LC and position_role(nm) in allowed:
            out.append({"id": p.id, "name": nm})
    out.sort(key=lambda x: x["name"])
    return out


def backfill_employee_position_stores(db):
    """One-time idempotent (Sam #2457 per-store positions): expand GLOBAL
    EmployeePosition rows (store_key NULL, pre-rework) into per-store rows - one
    per the employee's assigned stores (EmployeeStoreAssignment). A NULL row WITH
    stores is replaced by its per-store copies; a NULL row for a store-less
    employee is KEPT (never lose a position). Only touches NULL-store rows.
    Returns (expanded, removed). Safe at boot / re-run (re-run -> (0, 0))."""
    from collections import defaultdict
    null_rows = db.query(EmployeePosition).filter(EmployeePosition.store_key.is_(None)).all()
    if not null_rows:
        return (0, 0)
    emp_stores = defaultdict(list)
    for a in db.query(EmployeeStoreAssignment).all():
        sk = (a.store_key or "").strip().lower()
        if sk and sk not in emp_stores[a.employee_id]:
            emp_stores[a.employee_id].append(sk)
    existing = {(r.employee_id, r.position_id, r.store_key) for r in
                db.query(EmployeePosition).filter(EmployeePosition.store_key.isnot(None)).all()}
    expanded = removed = 0
    for r in null_rows:
        stores = emp_stores.get(r.employee_id, [])
        if not stores:
            continue  # store-less employee: keep the global row, don't lose the position
        for sk in stores:
            key = (r.employee_id, r.position_id, sk)
            if key not in existing:
                db.add(EmployeePosition(employee_id=r.employee_id,
                                        position_id=r.position_id, store_key=sk))
                existing.add(key)
                expanded += 1
        db.delete(r)
        removed += 1
    if expanded or removed:
        db.commit()
    return (expanded, removed)


# permission_level -> the canonical MANAGEMENT position name (layer-1 backfill).
_MGR_LEVEL_TO_POSITION = {
    "partner": "Partner", "corporate": "Corporate", "corporate_chef": "Corporate Chef",
    "gm": "GM", "km": "KM", "assistant_km": "Assistant KM", "foh_manager": "FOH Manager",
}


def backfill_manager_positions(db):
    """Layer-1 lockout-safety (Sam #2457 / ckai #2488 hole-1+2): assign each
    LINKED active manager (User.permission_level in the management set, with a
    linked Employee) the POSITION matching their level, at each store they're
    assigned to - so position-based enforcement finds their management perms
    (a manager with no position would otherwise lock out). store_key = the
    LOCATION key (tomball/copperfield, ckbro #2489 canonical key). Idempotent;
    must run at boot BEFORE enforcement is live. Returns assigned count."""
    pos_by_name = {}
    for p in db.query(Position).all():
        nm = (p.name or "").strip().lower()
        pos_by_name.setdefault(nm, p.id)
    existing = {(r.employee_id, r.position_id, r.store_key)
                for r in db.query(EmployeePosition).all()}
    from collections import defaultdict
    emp_stores = defaultdict(list)
    for a in db.query(EmployeeStoreAssignment).all():
        sk = (a.store_key or "").strip().lower()
        if sk and sk not in emp_stores[a.employee_id]:
            emp_stores[a.employee_id].append(sk)
    emp_by_user = {e.user_id: e for e in
                   db.query(Employee).filter(Employee.user_id.isnot(None)).all()}
    assigned = 0
    for u in db.query(User).filter(User.active.is_(True)).all():
        pos_name = _MGR_LEVEL_TO_POSITION.get((u.permission_level or "").strip().lower())
        if not pos_name:
            continue
        pid = pos_by_name.get(pos_name.lower())
        emp = emp_by_user.get(u.id)
        if pid is None or emp is None:
            continue
        for sk in emp_stores.get(emp.id, []):
            key = (emp.id, pid, sk)
            if key not in existing:
                db.add(EmployeePosition(employee_id=emp.id, position_id=pid, store_key=sk))
                existing.add(key)
                assigned += 1
    if assigned:
        db.commit()
    return assigned
