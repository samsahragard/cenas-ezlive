"""Probe HOLE-1 (HIGH, #2457): the per-store EmployeePosition migration must be
DIALECT-AWARE on SQLite. PROD is sqlite with an EXISTING populated
employee_positions that still has the OLD 2-col uq_emp_position(employee_id,
position_id) and NO store_key column. SQLite cannot ALTER DROP/ADD CONSTRAINT,
so the boot migration must do a TABLE REBUILD to the 3-col uq_emp_position_store
BEFORE the backfills run - otherwise the backfills raise IntegrityError on the
surviving 2-col uq and the feature is silently inert.

This probe replicates the PROD scenario (NOT a fresh db): it builds a COMPLETE
correct schema via create_all, then surgically rebuilds ONLY employee_positions
back to the OLD pre-rework shape (2-col uq, no store_key) and populates it - so
every other table is correct and only employee_positions is in the legacy state.
Then it points DATABASE_URL at that db and runs the boot migration via create_app.
The docck_tick_lease boot traceback (and any other unrelated 'non-fatal' boot
seed log) is benign - ignore it; this gate only asserts the migration outcome."""
import os, tempfile

os.environ["ALLOW_DEV_SECRET"] = "1"
DBP = os.path.join(tempfile.gettempdir(), "_hole1.db")
if os.path.exists(DBP):
    os.remove(DBP)
os.environ["DATABASE_URL"] = "sqlite:///" + DBP.replace("\\", "/")

# --- 1. Build a COMPLETE correct schema with create_all (NOT create_app, so the
# boot migration does NOT run yet), then surgically revert employee_positions to
# the OLD pre-rework shape + populate it. This yields the prod scenario: every
# table correct, only employee_positions legacy (2-col uq, no store_key). ---
from app.models import Base
from app.db import engine
from sqlalchemy import text

Base.metadata.create_all(engine)
with engine.begin() as conn:
    # Drop the freshly-created NEW employee_positions and rebuild the OLD shape:
    # 2-col UNIQUE(employee_id, position_id) named uq_emp_position, NO store_key.
    conn.execute(text("DROP TABLE employee_positions"))
    conn.execute(text(
        "CREATE TABLE employee_positions ("
        "id INTEGER NOT NULL PRIMARY KEY, "
        "employee_id INTEGER NOT NULL, "
        "position_id INTEGER NOT NULL, "
        "created_at DATETIME NOT NULL, "
        "CONSTRAINT uq_emp_position UNIQUE (employee_id, position_id), "
        "FOREIGN KEY(employee_id) REFERENCES employees (id) ON DELETE CASCADE, "
        "FOREIGN KEY(position_id) REFERENCES positions (id) ON DELETE CASCADE)"))
    conn.execute(text("CREATE INDEX ix_employee_positions_employee_id "
                      "ON employee_positions (employee_id)"))
    conn.execute(text("CREATE INDEX ix_employee_positions_position_id "
                      "ON employee_positions (position_id)"))
    # Seed a GM position + a generic Cook position (global rows, store_key NULL).
    conn.execute(text("INSERT INTO positions (name, store_key, created_at) VALUES "
                      "('GM', NULL, '2026-01-01 00:00:00'), "
                      "('Cook', NULL, '2026-01-01 00:00:00')"))
    # Legacy employee 'Legacy' with a GLOBAL Cook position (the pre-rework row).
    conn.execute(text(
        "INSERT INTO employees (full_name, active, created_at, updated_at, "
        "session_version, failed_attempts) VALUES "
        "('Legacy', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00', 0, 0)"))

FAILS = []
def chk(n, c, e=""):
    print(("PASS " if c else "FAIL "), n, ("| " + str(e)) if e else "")
    if not c:
        FAILS.append(n)

# Resolve the seeded ids + insert the legacy global EmployeePosition row.
from sqlalchemy import inspect as sa_inspect
with engine.begin() as conn:
    cook_id = conn.execute(text("SELECT id FROM positions WHERE name='Cook'")).scalar()
    legacy_id = conn.execute(text("SELECT id FROM employees WHERE full_name='Legacy'")).scalar()
    conn.execute(text(
        "INSERT INTO employee_positions (employee_id, position_id, created_at) "
        "VALUES (:e, :p, '2026-01-01 00:00:00')"), {"e": legacy_id, "p": cook_id})

# PRE sanity: confirm we truly built the OLD shape (use the inspector, which reads
# the named CONSTRAINT from the table DDL - raw PRAGMA only shows the autoindex).
_insp0 = sa_inspect(engine)
_pre_cols = {c["name"] for c in _insp0.get_columns("employee_positions")}
_pre_uqs = {u.get("name") for u in _insp0.get_unique_constraints("employee_positions")}
with engine.connect() as conn:
    _pre_rows = conn.execute(text("SELECT COUNT(*) FROM employee_positions")).scalar()
print("PRE  old-shape employee_positions: cols=%s uqs=%s rows=%d"
      % (sorted(_pre_cols), sorted(_pre_uqs), _pre_rows))
chk("PRE old 2-col uq_emp_position present, no store_key, populated",
    "uq_emp_position" in _pre_uqs and "store_key" not in _pre_cols and _pre_rows == 1,
    (_pre_uqs, _pre_cols, _pre_rows))

# --- 2. create_app runs the boot migration against the OLD populated db ---
from app import create_app
app = create_app()
from app.db import SessionLocal
from app.models import (Employee, EmployeePosition, EmployeeStoreAssignment,
                        Position, User)
from werkzeug.security import generate_password_hash

# --- 3a. Assert the migration rebuilt the table to the NEW shape ---
insp = sa_inspect(engine)
cols = {c["name"] for c in insp.get_columns("employee_positions")}
uqs = {u.get("name") for u in insp.get_unique_constraints("employee_positions")}
chk("a store_key column now exists", "store_key" in cols, sorted(cols))
chk("b1 new 3-col uq_emp_position_store in place", "uq_emp_position_store" in uqs, sorted(uqs))
chk("b2 old 2-col uq_emp_position is GONE", "uq_emp_position" not in uqs, sorted(uqs))
uq_cols = {tuple(u.get("column_names") or [])
           for u in insp.get_unique_constraints("employee_positions")
           if u.get("name") == "uq_emp_position_store"}
chk("b3 uq_emp_position_store spans (employee_id, position_id, store_key)",
    uq_cols == {("employee_id", "position_id", "store_key")}, uq_cols)
chk("b4 indexes recreated on employee_id, position_id, store_key after rebuild",
    {i["name"] for i in insp.get_indexes("employee_positions")} >=
    {"ix_employee_positions_employee_id", "ix_employee_positions_position_id",
     "ix_employee_positions_store_key"},
    {i["name"] for i in insp.get_indexes("employee_positions")})
# legacy data survived the rebuild (still the single global Cook row at this point)
db = SessionLocal()
legacy_id = db.query(Employee).filter_by(full_name="Legacy").first().id
cook_pid = db.query(Position).filter_by(name="Cook").first().id
lg = db.query(EmployeePosition).filter_by(employee_id=legacy_id, position_id=cook_pid).all()
chk("data legacy global Cook row survived the rebuild (store NULL, 1 row)",
    len(lg) == 1 and lg[0].store_key is None, [(r.id, r.store_key) for r in lg])

# --- 3b. Seed the PROD collision scenario: give the legacy employee BOTH store
# assignments (its global row must expand to 2 per-store rows), and add a
# both-store GM manager (User gm linked to Employee, assigned to BOTH stores).
# These are the EXACT collisions that raised IntegrityError on the surviving
# 2-col uq pre-fix. ---
db.add(EmployeeStoreAssignment(employee_id=legacy_id, store_key="tomball"))
db.add(EmployeeStoreAssignment(employee_id=legacy_id, store_key="copperfield"))
mgr = Employee(full_name="BothGM", active=True); db.add(mgr); db.flush()
um = User(full_name="BothGM", email="bothgm@x.com", phone=None,
          passcode_hash=generate_password_hash("12345"), permission_level="gm",
          store_scope="both", first_login_done=True, active=True, session_version=0)
db.add(um); db.flush()
mgr.user_id = um.id
db.add(EmployeeStoreAssignment(employee_id=mgr.id, store_key="tomball"))
db.add(EmployeeStoreAssignment(employee_id=mgr.id, store_key="copperfield"))
db.commit()
mgr_id = mgr.id
gm_pid = db.query(Position).filter_by(name="GM").first().id

# --- 3c. Run the backfills (same call order as boot). With the 3-col uq in place
# they must NOT raise. (Boot already ran them once at create_app over the seed
# present then; here we drive them against the freshly-seeded collision case.) ---
from app.services.team_roster import (backfill_manager_positions,
                                       backfill_employee_position_stores)
raised = None
try:
    backfill_manager_positions(db)
    backfill_employee_position_stores(db)
except Exception as ex:
    raised = ex
chk("c backfills ran WITHOUT raising on the populated (formerly 2-col-uq) table",
    raised is None, raised)

# --- 3d. Both-store manager has GM rows for BOTH stores (the manager collision). ---
mgr_stores = {r.store_key for r in
              db.query(EmployeePosition).filter_by(employee_id=mgr_id, position_id=gm_pid).all()}
chk("d both-store manager got GM position at BOTH tomball AND copperfield",
    mgr_stores == {"tomball", "copperfield"}, mgr_stores)

# --- 3c2. Legacy global Cook row expanded to per-store, NULL row gone (the
# global-expansion collision). ---
legacy_stores = {r.store_key for r in
                 db.query(EmployeePosition).filter_by(employee_id=legacy_id, position_id=cook_pid).all()}
chk("c2 legacy global Cook row expanded to per-store (tomball+copperfield, no NULL)",
    legacy_stores == {"tomball", "copperfield"}, legacy_stores)

# --- 3e. Idempotent: re-run create_app + the backfills -> no error, no dup rows.
# (NB: a default partner User 'Sam' is auto-seeded at boot and legitimately gets a
# Partner position at both stores, so the GLOBAL row total is not a fixed number;
# idempotency is measured as 'the backfill re-run adds zero rows', and we assert
# there are no duplicate (employee, position, store) tuples anywhere.) ---
db.close()
app2 = create_app()             # re-run the whole boot migration over the migrated db
db = SessionLocal()
uqs2 = {u.get("name") for u in sa_inspect(engine).get_unique_constraints("employee_positions")}
chk("e1 re-run create_app kept the 3-col uq (sqlite rebuild SKIPPED, no error)",
    "uq_emp_position_store" in uqs2 and "uq_emp_position" not in uqs2, sorted(uqs2))
before_count = db.query(EmployeePosition).count()   # after boot has fully stabilized
re_mgr = backfill_manager_positions(db)
re_exp, re_rem = backfill_employee_position_stores(db)
chk("e2 backfills idempotent on re-run -> (0,0,0)", (re_mgr, re_exp, re_rem) == (0, 0, 0),
    (re_mgr, re_exp, re_rem))
after_count = db.query(EmployeePosition).count()
chk("e3 backfill re-run added no rows (idempotent)", after_count == before_count,
    (before_count, after_count))

# --- 3f. No rows lost + no dups. Scope the exact counts to the TEST entities
# (the seeded partner adds its own legitimate rows), and assert the global
# invariants: no duplicate (emp,pos,store) tuple, no NULL-store row for any
# store-assigned employee. ---
legacy_rows = db.query(EmployeePosition).filter_by(employee_id=legacy_id).all()
mgr_rows = db.query(EmployeePosition).filter_by(employee_id=mgr_id).all()
chk("f1 legacy employee has exactly its 2 per-store Cook rows (no loss, no dup)",
    {(r.position_id, r.store_key) for r in legacy_rows}
    == {(cook_pid, "tomball"), (cook_pid, "copperfield")},
    [(r.position_id, r.store_key) for r in legacy_rows])
chk("f2 both-store manager has exactly its 2 per-store GM rows (no loss, no dup)",
    {(r.position_id, r.store_key) for r in mgr_rows}
    == {(gm_pid, "tomball"), (gm_pid, "copperfield")},
    [(r.position_id, r.store_key) for r in mgr_rows])
all_rows = db.query(EmployeePosition).all()
tuples = [(r.employee_id, r.position_id, r.store_key) for r in all_rows]
chk("f3 NO duplicate (employee, position, store) tuples anywhere",
    len(tuples) == len(set(tuples)), len(tuples) - len(set(tuples)))
store_assigned = {a.employee_id for a in db.query(EmployeeStoreAssignment).all()}
bad_null = [(r.employee_id, r.position_id) for r in all_rows
            if r.store_key is None and r.employee_id in store_assigned]
chk("f4 no leftover NULL-store row for any store-assigned employee", bad_null == [], bad_null)
db.close()

print()
print("=== HOLE-1 PER-STORE POSITIONS MIGRATION GATE:",
      "ALL PASS" if not FAILS else "%d FAIL %s" % (len(FAILS), FAILS), "===")
