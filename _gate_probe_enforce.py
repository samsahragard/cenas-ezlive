"""Probe: the permission ENFORCEMENT repoint (perms-rework) — _user_has now
reads the POSITION-based effective set (saved PositionPermission union at the
active store) gated BEHIND the static role-perms fallback, lockout-safe.

Covers the 5-hole spec:
  LOCKOUT a — gm-linked Employee, NO positions, active_store=None -> role-perms.
  LOCKOUT b — same gm, active_store=tomball but no positions there -> role-perms.
  POSITIVE  — saved PositionPermission for (gm, tomball) drives enforcement:
              a distinctive perm NOT in gm's role-default is GRANTED via the
              SAVED config (proves the join, not the fallback masking it), and
              a perm left OFF + not in gm's default is DENIED.
  PARTNER   — permission_level=partner -> True for ANY tag (wildcard), even with
              no positions.
The docck_tick_lease traceback at boot is benign — ignore it.
"""
import os, tempfile
os.environ["ALLOW_DEV_SECRET"] = "1"
DBP = os.path.join(tempfile.gettempdir(), "_enforce.db")
if os.path.exists(DBP):
    os.remove(DBP)
os.environ["DATABASE_URL"] = "sqlite:///" + DBP.replace("\\", "/")
from app import create_app
app = create_app()                       # A: boot proves imports + table-create
from app.db import SessionLocal
from app.models import (User, Employee, EmployeePosition, Position,
                        PositionPermission)
from app.services.permissions import _user_has, ROLE_PERMISSIONS
from app.services.permission_catalog import default_role_map
from werkzeug.security import generate_password_hash

FAILS = []
def chk(n, c, extra=""):
    print(("PASS " if c else "FAIL "), n, ("| " + str(extra)) if extra else "")
    if not c:
        FAILS.append(n)

print("A boot: create_app() succeeded")

# Tags used by the probe. NOTE the two distinct vocabularies:
#   * the static ROLE_PERMISSIONS fallback uses tags like
#     'labor.view_store_summary' (the lockout-fallback proof);
#   * the position-union (catalog / default_role_map + PositionPermission)
#     uses catalog tags like 'emp.view_wages' / 'fin.view_pnl' / 'reports.labor'.
# The lockout tag MUST come from ROLE_PERMISSIONS (hole 3 = the role-perms
# fallback is the static dict), so we pick one that's in ROLE_PERMISSIONS['gm']
# but NOT in the catalog gm-default -> it can ONLY be granted via the fallback.
ROLE_FALLBACK_TAG = "labor.view_store_summary"  # in ROLE_PERMISSIONS['gm'], NOT catalog gm-default
SAVED_ONLY_TAG    = "emp.view_wages"   # catalog tag NOT in gm's catalog-default -> only SAVED config grants it
SAVED_CAT_TAG     = "reports.labor"    # catalog tag IS in gm's catalog-default; also seeded ON
OFF_TAG           = "fin.view_pnl"     # catalog tag left OFF + NOT in gm's catalog-default -> must be DENIED

drm = default_role_map()
gm_cat_def = drm.get("gm", set())
gm_role = ROLE_PERMISSIONS.get("gm", set())
chk("pre: labor.view_store_summary IS in ROLE_PERMISSIONS[gm] (the fallback)", ROLE_FALLBACK_TAG in gm_role)
chk("pre: labor.view_store_summary NOT in catalog gm-default (so only fallback grants it)", ROLE_FALLBACK_TAG not in gm_cat_def)
chk("pre: emp.view_wages NOT a gm catalog-default", SAVED_ONLY_TAG not in gm_cat_def)
chk("pre: fin.view_pnl NOT a gm catalog-default", OFF_TAG not in gm_cat_def)

# --- seed ---
db = SessionLocal()
for M in (PositionPermission, EmployeePosition, Employee, User):
    db.query(M).delete()
db.commit()

def _gm_user(email):
    u = User(full_name="GM", email=email, phone=None,
             passcode_hash=generate_password_hash("12345"),
             permission_level="gm", store_scope="tomball,copperfield",
             first_login_done=True, active=True, session_version=0)
    db.add(u); db.flush()
    return u

# LOCKOUT user: a gm-linked Employee with NO positions anywhere.
u_lock = _gm_user("lock@x.com")
e_lock = Employee(full_name="GM Lock", email="lock@x.com", active=True,
                  user_id=u_lock.id)
db.add(e_lock); db.flush()
lock_uid = u_lock.id

# POSITIVE user: a gm-linked Employee assigned the GM position @ tomball.
u_pos = _gm_user("pos@x.com")
e_pos = Employee(full_name="GM Pos", email="pos@x.com", active=True,
                 user_id=u_pos.id)
db.add(e_pos); db.flush()
gm_pid = db.query(Position).filter(Position.name == "GM").first().id
db.add(EmployeePosition(employee_id=e_pos.id, position_id=gm_pid,
                        store_key="tomball"))
# Saved config for (gm, tomball): a distinctive grant the gm catalog-default
# does NOT contain (SAVED_ONLY_TAG) + a gm catalog-default tag (SAVED_CAT_TAG).
# OFF_TAG is deliberately NOT seeded.
db.add(PositionPermission(position_key="gm", store_key="tomball",
                          perm_key=SAVED_ONLY_TAG))
db.add(PositionPermission(position_key="gm", store_key="tomball",
                          perm_key=SAVED_CAT_TAG))
pos_uid = u_pos.id

# PARTNER user: no positions at all.
u_par = User(full_name="Partner", email="par@x.com", phone=None,
             passcode_hash=generate_password_hash("12345"),
             permission_level="partner", store_scope=None,
             first_login_done=True, active=True, session_version=0)
db.add(u_par); db.commit()
par_uid = u_par.id

# Re-fetch the User rows to pass real rows into _user_has.
U_LOCK = db.get(User, lock_uid)
U_POS  = db.get(User, pos_uid)
U_PAR  = db.get(User, par_uid)
db.close()

# A request context is required so flask.session / flask.g resolve.
def under(active_store):
    """Return a fresh test_request_context with session['active_store'] set."""
    ctx = app.test_request_context("/")
    return ctx

# --- LOCKOUT a: gm, NO positions, active_store=None -> role-perms (True) ---
from flask import session as _sess
with app.test_request_context("/"):
    _sess.pop("active_store", None)   # unset
    a_ok = _user_has(U_LOCK, ROLE_FALLBACK_TAG)
chk("LOCKOUT a: gm no-positions + active_store=None -> role-perms tag GRANTED via fallback (no crash)",
    a_ok is True, a_ok)

# --- LOCKOUT b: gm, active_store=tomball but no positions there -> role-perms ---
with app.test_request_context("/"):
    _sess["active_store"] = "tomball"
    b_ok = _user_has(U_LOCK, ROLE_FALLBACK_TAG)
chk("LOCKOUT b: gm active_store=tomball + no positions there -> STILL role-perms (True)",
    b_ok is True, b_ok)

# --- POSITIVE: saved config @ (gm, tomball) drives enforcement ---
with app.test_request_context("/"):
    _sess["active_store"] = "tomball"
    saved_ok = _user_has(U_POS, SAVED_ONLY_TAG)   # only SAVED config can grant
with app.test_request_context("/"):
    _sess["active_store"] = "tomball"
    catdef_ok = _user_has(U_POS, SAVED_CAT_TAG)    # in saved config too
with app.test_request_context("/"):
    _sess["active_store"] = "tomball"
    off_denied = _user_has(U_POS, OFF_TAG)         # not saved, not catalog-default
chk("POSITIVE 1: saved-only perm (not in gm catalog-default) GRANTED via SAVED config @ tomball (proves the join)",
    saved_ok is True, saved_ok)
chk("POSITIVE 2: the distinctive saved catalog-default perm (reports.labor) GRANTED @ tomball",
    catdef_ok is True, catdef_ok)
chk("POSITIVE 3: perm left OFF for (gm,tomball) + NOT in gm catalog-default -> DENIED (saved config drives it)",
    off_denied is False, off_denied)

# Cross-store proof: the SAME gm at a DIFFERENT active store (copperfield) has
# no positions there -> falls back to role-perms, so the saved-only tomball
# perm is NOT granted (the saved config is store-scoped, fallback is the role).
with app.test_request_context("/"):
    _sess["active_store"] = "copperfield"
    cross = _user_has(U_POS, SAVED_ONLY_TAG)
chk("POSITIVE 4: same gm @ copperfield (no positions there) -> saved-only tomball perm NOT granted (store-scoped)",
    cross is False, cross)

# --- PARTNER: wildcard True for ANY tag, even with no positions ---
with app.test_request_context("/"):
    _sess.pop("active_store", None)
    p1 = _user_has(U_PAR, OFF_TAG)
    p2 = _user_has(U_PAR, "some.tag.that.does.not.exist")
chk("PARTNER: wildcard GRANTS any tag with no active_store / no positions (p1)", p1 is True, p1)
chk("PARTNER: wildcard GRANTS a nonexistent tag (p2)", p2 is True, p2)

print()
print("=== ENFORCEMENT REPOINT GATE:",
      "ALL PASS" if not FAILS else "%d FAIL %s" % (len(FAILS), FAILS), "===")
