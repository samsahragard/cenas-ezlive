"""scripts/sling_migrate.py - Schedules V2 Block 3 (Sam #1742).

Migrate the Sling "People" CSV export into the B2 employee tables
(employees, positions, employee_positions, employee_store_assignments).

IMPORT-ALL strategy (Sam #1809 = "1"), exact rules locked with samai #1812 +
ckai #1810 (the normalize_phone() = B2 login auth contract):

  - normalize_phone(raw) = digits only, drop a leading "1" on 11-digit numbers
    -> canonical 10-digit. A 12-digit / non-"1" 11-digit does NOT canonicalize.
    MUST match app/web/employee_auth.py so a migrated phone hits at SMS login.

  - employees.phone is NULLABLE + UNIQUE (B3 schema change). Store the canonical
    10-digit phone, ELSE NULL + punch-list:
       blank            -> NULL, flag "no-phone"
       not 10 post-norm -> NULL, flag "malformed-phone"   (e.g. Aniya Owens id 322, 12-digit)
       shared-phone twin-> NULL, flag "shared-phone-yielded"

  - DEDUPE, two stages, in order:
      1. EXACT-DUPLICATE rows (same EMPLOYEE ID) -> merge (keep one). The two
         twins (sling_id 9760, 9930) collapse 110 rows -> 108 logical employees.
      2. GENUINE shared-phone pairs (DIFFERENT people, same canonical phone -> 3
         pairs): the STATUS=Joined member keeps the phone; the non-Joined twin
         gets phone=NULL. Done AFTER step 1 so a twin's own duplicate row never
         looks like a "shared phone".

  - active = (STATUS == "Joined"); the 16 non-Joined import active=False.
    Decoupled from phone: a Joined employee with no phone is active + NULL phone
    (in-system, no login until a phone is set).

  - sling_id <- EMPLOYEE ID (blank -> NULL). POSITIONS: comma-split + canonicalize
    ("WIndow"->Window, "kitchen manager tomball"->Kitchen Manager) -> Position +
    EmployeePosition. LOCATIONS -> store_key (tomball / copperfield; "both" -> 2;
    blank -> none). GROUPS: ignored (empty in the export).

  - Idempotent: upsert keyed on sling_id when present, else canonical phone.
    Re-running updates in place (0 new rows). tx/rollback on any error. NO silent
    drops: every merge / null / flag / skip is logged + counted.

TEST-DB-FIRST: `python scripts/sling_migrate.py` builds a THROWAWAY in-memory
sqlite, runs the migration, prints the report, touches NO real DB. run_migration()
is the same entry point /partner/schedules-v2/migration/run calls (commit=True,
live session) after the dry-run counts pass samai's gate.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# repo root on sys.path so `import app.models` resolves when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STORE_MAP = {
    "27727 tomball parkway": "tomball",
    "fm 529 - copperfield": "copperfield",
}
# position canonicalization (lowercased lookup key -> canonical display name)
POSITION_CANON = {
    "window": "Window",
    "kitchen manager tomball": "Kitchen Manager",
    "well": "Bar Well",  # samai #1839: "Well" + "Bar Well" are the same bar station
}
ACTIVE_STATUSES = {"joined"}
MODEL_NAMES = ("Employee", "Position", "EmployeePosition", "EmployeeStoreAssignment")


def normalize_phone(raw: str) -> str:
    """Digits-only; drop a leading US '1' on 11-digit numbers. Canonical 10-digit
    or '' / a non-canonical run. Mirrors app/web/employee_auth.py EXACTLY."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def map_store(raw: str):
    out = []
    for part in (raw or "").split(","):
        key = STORE_MAP.get(part.strip().lower())
        if key and key not in out:
            out.append(key)
    return out


def canon_positions(raw: str):
    out = []
    for p in (raw or "").split(","):
        p = p.strip()
        if not p:
            continue
        name = POSITION_CANON.get(p.lower(), p)
        if name not in out:
            out.append(name)
    return out


def build_plan(rows):
    """Pure (no DB). Returns (plan, info) where plan = 108 logical employee dicts
    and info = {'merged_sling_ids': [...], 'csv_rows': N}."""
    # --- stage 1: collapse same-EMPLOYEE-ID rows, KEEPING THE JOINED ROW ---
    # (samai #1812: each dup pair is 1 Joined + 1 Invited copy; keep the Joined.)
    groups = {}
    nullsid = []
    for r in rows:
        sid = (r.get("EMPLOYEE ID") or "").strip()
        if sid:
            groups.setdefault(sid, []).append(r)
        else:
            nullsid.append(r)
    merged = []
    logical = []
    for sid, grp in groups.items():
        if len(grp) > 1:
            merged.append(sid)
            keep = next((g for g in grp
                         if (g.get("STATUS", "") or "").strip().lower() in ACTIVE_STATUSES), grp[0])
            logical.append(keep)
        else:
            logical.append(grp[0])
    logical.extend(nullsid)

    # --- stage 2: canonical-phone collisions AMONG the deduped logical rows ---
    norm = {id(r): normalize_phone(r.get("PHONE", "")) for r in logical}
    pcount = Counter(p for p in norm.values() if p and len(p) == 10)
    shared = {p for p, c in pcount.items() if c > 1}
    # keeper of each shared phone = the first Joined member (Joined keeps it)
    keeper = {}
    for r in logical:
        ph = norm[id(r)]
        if ph in shared and ph not in keeper:
            if (r.get("STATUS", "") or "").strip().lower() in ACTIVE_STATUSES:
                keeper[ph] = id(r)

    plan = []
    for r in logical:
        ph = norm[id(r)]
        joined = (r.get("STATUS", "") or "").strip().lower() in ACTIVE_STATUSES
        flags = []
        phone_val = None
        if not ph:
            flags.append("no-phone")
        elif len(ph) != 10:
            flags.append("malformed-phone")
        elif ph in shared:
            if keeper.get(ph) == id(r):
                phone_val = ph
            else:
                flags.append("shared-phone-yielded")
        else:
            phone_val = ph
        plan.append({
            "full_name": (r.get("NAME") or "").strip(),
            "phone": phone_val,                       # canonical-10 or None
            "email": (r.get("EMAIL") or "").strip() or None,
            "sling_id": (r.get("EMPLOYEE ID") or "").strip() or None,
            "active": joined,
            "positions": canon_positions(r.get("POSITIONS", "")),
            "stores": map_store(r.get("LOCATIONS", "")),
            "flags": flags,
        })
    return plan, {"merged_sling_ids": merged, "csv_rows": len(rows)}


def run_migration(rows, session, models, commit=False, log=print):
    """Apply the plan to `session` (idempotent upsert). Returns (report,
    flag_detail, info). Rolls back on error; commits only if commit=True + clean."""
    Employee = models["Employee"]
    Position = models["Position"]
    EmployeePosition = models["EmployeePosition"]
    EmployeeStoreAssignment = models["EmployeeStoreAssignment"]

    plan, info = build_plan(rows)
    for sid in info["merged_sling_ids"]:
        log("MERGE exact-duplicate row: sling_id=%s (kept 1)" % sid)
    rep = Counter()
    pos_cache = {p.name: p for p in session.query(Position).all()}
    try:
        for row in plan:
            now = datetime.utcnow()
            emp = None
            if row["sling_id"]:
                emp = session.query(Employee).filter(Employee.sling_id == row["sling_id"]).first()
            if emp is None and row["phone"]:
                emp = session.query(Employee).filter(Employee.phone == row["phone"]).first()
            if emp is None and not row["sling_id"] and not row["phone"]:
                # No stable key (no sling_id, no phone) -> match on name + both-null
                # so a re-run UPDATES instead of duplicating (idempotency). These are
                # the incomplete edge rows; name is the only remaining identifier.
                emp = session.query(Employee).filter(
                    Employee.full_name == row["full_name"],
                    Employee.sling_id.is_(None),
                    Employee.phone.is_(None),
                ).first()
            if emp is None:
                emp = Employee(created_at=now)
                session.add(emp)
                rep["employees_created"] += 1
            else:
                rep["employees_updated"] += 1
            emp.full_name = row["full_name"]
            emp.phone = row["phone"]
            emp.email = row["email"]
            emp.sling_id = row["sling_id"]
            emp.active = row["active"]
            emp.updated_at = now
            session.flush()

            rep["active" if row["active"] else "inactive"] += 1
            if row["phone"] is None:
                rep["phone_null"] += 1
            if row["flags"]:
                rep["flagged"] += 1
                log("FLAG %s [%s] sling_id=%s" % (row["full_name"], ",".join(row["flags"]), row["sling_id"]))

            for pname in row["positions"]:
                pos = pos_cache.get(pname)
                if pos is None:
                    pos = Position(name=pname, created_at=now)
                    session.add(pos)
                    session.flush()
                    pos_cache[pname] = pos
                    rep["positions_created"] += 1
                if session.query(EmployeePosition).filter_by(
                        employee_id=emp.id, position_id=pos.id).first() is None:
                    session.add(EmployeePosition(
                        employee_id=emp.id, position_id=pos.id, created_at=now))
                    rep["employee_positions"] += 1

            for sk in row["stores"]:
                if session.query(EmployeeStoreAssignment).filter_by(
                        employee_id=emp.id, store_key=sk).first() is None:
                    session.add(EmployeeStoreAssignment(
                        employee_id=emp.id, store_key=sk, created_at=now))
                    rep["store_assignments"] += 1

        if commit:
            session.commit()
            rep["committed"] = 1
        else:
            session.rollback()
            rep["dry_run"] = 1
    except Exception:
        session.rollback()
        raise

    flag_detail = Counter()
    for row in plan:
        for f in row["flags"]:
            flag_detail[f] += 1
    return dict(rep), dict(flag_detail), info


def _models_ns():
    import app.models as m
    base = getattr(m, "Base", None)
    if base is None:
        from app.db import Base as base  # noqa
    return m, base, {n: getattr(m, n) for n in MODEL_NAMES}


def _load_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Sling -> B2 employee migration (B3).")
    ap.add_argument("--csv", default=r"C:\Users\Public\sling_export.csv")
    ap.add_argument("--commit", action="store_true",
                    help="write for real against --db (default: dry-run on a throwaway sqlite)")
    ap.add_argument("--db", help="SQLAlchemy URL for --commit (the live DB).")
    args = ap.parse_args(argv)

    rows = _load_csv(args.csv)
    m, Base, models = _models_ns()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    if args.commit:
        if not args.db:
            ap.error("--commit requires --db (the live DB URL)")
        engine = create_engine(args.db)
    else:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)

    session = sessionmaker(bind=engine)()
    rep, flags, info = run_migration(rows, session, models, commit=args.commit)

    print("=== SLING MIGRATE: %s ===" % ("COMMIT" if args.commit else "DRY-RUN (throwaway sqlite)"))
    print("csv rows: %d -> logical employees: %d (merged exact-dups: %d)"
          % (info["csv_rows"], info["csv_rows"] - len(info["merged_sling_ids"]), len(info["merged_sling_ids"])))
    for k in sorted(rep):
        print("  %-22s %s" % (k, rep[k]))
    print("flag detail:")
    for k in sorted(flags):
        print("  %-22s %s" % (k, flags[k]))
    return rep


if __name__ == "__main__":
    main()
