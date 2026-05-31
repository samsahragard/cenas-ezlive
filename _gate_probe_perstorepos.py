"""Probe: EmployeePosition+store model (new schema via create_all) + the
backfill (global store_key=NULL rows -> per-store), Sam #2457."""
import os, tempfile
os.environ["ALLOW_DEV_SECRET"] = "1"
DBP = os.path.join(tempfile.gettempdir(), "_psp.db")
if os.path.exists(DBP):
    os.remove(DBP)
os.environ["DATABASE_URL"] = "sqlite:///" + DBP.replace("\\", "/")
from app import create_app
app = create_app()
from app.db import SessionLocal
from app.models import Employee, EmployeePosition, EmployeeStoreAssignment, Position
from app.services.team_roster import backfill_employee_position_stores

FAILS = []
def chk(n, c, e=""):
    print(("PASS " if c else "FAIL "), n, ("| " + str(e)) if e else "")
    if not c:
        FAILS.append(n)

print("A boot ok (new EmployeePosition schema created via create_all)")
db = SessionLocal()
for M in (EmployeePosition, EmployeeStoreAssignment, Employee):
    db.query(M).delete()
db.commit()
pid = db.query(Position).first().id   # any canonical position (boot-seeded)
# Maria: a GLOBAL position (store_key NULL) + assigned to BOTH stores
e = Employee(full_name="Maria", email="m@x.com", active=True); db.add(e); db.flush()
db.add(EmployeePosition(employee_id=e.id, position_id=pid, store_key=None))
db.add(EmployeeStoreAssignment(employee_id=e.id, store_key="tomball"))
db.add(EmployeeStoreAssignment(employee_id=e.id, store_key="copperfield"))
# Solo: a global position but NO store assignment (row must be KEPT, not lost)
e2 = Employee(full_name="Solo", email="s@x.com", active=True); db.add(e2); db.flush()
db.add(EmployeePosition(employee_id=e2.id, position_id=pid, store_key=None))
db.commit()

exp, rem = backfill_employee_position_stores(db)
chk("backfill expanded=2 removed=1 (Maria global -> 2 per-store)", exp == 2 and rem == 1, (exp, rem))
stores = {r.store_key for r in db.query(EmployeePosition).filter_by(employee_id=e.id).all()}
chk("Maria now 2 per-store rows (tomball+copperfield, no NULL)", stores == {"tomball", "copperfield"}, stores)
solo = db.query(EmployeePosition).filter_by(employee_id=e2.id).all()
chk("store-less Solo's global row KEPT (position not lost)", len(solo) == 1 and solo[0].store_key is None, [r.store_key for r in solo])
exp2, rem2 = backfill_employee_position_stores(db)
chk("idempotent re-run -> (0,0)", (exp2, rem2) == (0, 0), (exp2, rem2))

# --- layer-1 manager-position backfill ---
from app.models import User
from app.services.team_roster import backfill_manager_positions
from werkzeug.security import generate_password_hash
mgr = Employee(full_name="GMgr", email="gmgr@x.com", active=True); db.add(mgr); db.flush()
um = User(full_name="GMgr", email="gmgr@x.com", phone=None, passcode_hash=generate_password_hash("12345"),
          permission_level="gm", store_scope="tomball", first_login_done=True, active=True, session_version=0)
db.add(um); db.flush(); mgr.user_id = um.id
db.add(EmployeeStoreAssignment(employee_id=mgr.id, store_key="tomball")); db.commit()
nm = backfill_manager_positions(db)
gm_pid = db.query(Position).filter(Position.name == "GM").first().id
chk("J manager-backfill: GM manager got GM position @ tomball (location key)",
    db.query(EmployeePosition).filter_by(employee_id=mgr.id, position_id=gm_pid, store_key="tomball").count() == 1, nm)
chk("J manager-backfill idempotent (re-run 0)", backfill_manager_positions(db) == 0)
db.close()
print()
print("=== PER-STORE POSITIONS BACKFILL GATE:", "ALL PASS" if not FAILS else "%d FAIL %s" % (len(FAILS), FAILS), "===")
