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
                        EmployeeStoreAssignment, Position, User)
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
    """Idempotent unify backfill (Sam #2261, ckai seam #2295): link each ACTIVE
    User to an Employee matched by email - or CREATE + link + store-assign an
    Employee for a pure manager who has none, so managers/partners appear in the
    one team roster + are schedulable. The link (Employee.user_id) is additive;
    the User row + keypad auth are untouched. A created manager gets one
    EmployeeStoreAssignment per store in their User.store_scope (NULL/'both' ->
    both stores). Skips an already-linked User (User.email is UNIQUE so each
    email-match is 1:1). Returns (linked, created). Safe to call at boot or re-run.
    """
    emps = db.query(Employee).all()
    already = {e.user_id for e in emps if e.user_id is not None}
    by_email = {}
    for e in emps:
        if e.email:
            by_email.setdefault(e.email.strip().lower(), e)
    linked = created = 0
    for u in db.query(User).filter(User.active.is_(True)).all():
        if u.id in already:
            continue
        ue = (u.email or "").strip().lower()
        emp = by_email.get(ue) if ue else None
        if emp is None:
            emp = Employee(full_name=u.full_name, email=u.email, phone=None, active=True)
            db.add(emp)
            db.flush()
            created += 1
            scope = (u.store_scope or "").strip().lower()
            stores = (["tomball", "copperfield"] if scope in ("", "both")
                      else [scope] if scope in ("tomball", "copperfield") else [])
            for sk in stores:
                db.add(EmployeeStoreAssignment(employee_id=emp.id, store_key=sk))
        if emp.user_id is None:
            emp.user_id = u.id
            linked += 1
    if linked or created:
        db.commit()
    return linked, created
