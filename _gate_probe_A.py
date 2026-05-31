"""Probe: perms-rework PIECE A - catalog toggles are now AUTHORITATIVE (incl OFF
revoking) for the route tags they are explicitly bound to (CATALOG_TO_TAGS /
MANAGED_TAGS), while every UNbound route tag stays on the role baseline (no
lockout). This closes the additive-only gap where an OFF toggle could not revoke.

The bridge (permissions.py):
    CATALOG_TO_TAGS = {
        'emp.reset_passcode': {'drivers.reset_passcode'},
        'legal.upload_docs':  {'legal.upload_document'},
        'dash.dev_chat':      {'developer.view_chat'},
        'legal.view_insurance': {'legal.view_insurance'},
    }
    MANAGED_TAGS = union of the values above.

Coverage:
  OFF-REVOKE  - corporate-positioned user @ tomball whose saved PositionPermission
                config OMITS dash.dev_chat -> developer.view_chat (MANAGED) REVOKED;
                then dash.dev_chat ON -> GRANTED.
  PASS-THRU   - same user, team_reports.view is NOT managed -> stays role-baseline
                TRUE regardless of the catalog.
  BELT        - gm with NO positions at active store + active_store=None -> full
                role baseline; a MANAGED tag the role grants is NOT wrongly revoked.
  PARTNER     - permission_level=partner -> wildcard TRUE for any tag.
  MANAGED_TAGS == the exact 4-tag set.

The docck_tick_lease traceback at boot is benign - ignore it.
"""
import os, tempfile
os.environ["ALLOW_DEV_SECRET"] = "1"
DBP = os.path.join(tempfile.gettempdir(), "_gateA.db")
if os.path.exists(DBP):
    os.remove(DBP)
os.environ["DATABASE_URL"] = "sqlite:///" + DBP.replace("\\", "/")
from app import create_app
app = create_app()                       # boot proves imports + table-create + seed
from app.db import SessionLocal
from app.models import (User, Employee, EmployeePosition, Position,
                        PositionPermission)
from app.services.permissions import (_user_has, ROLE_PERMISSIONS,
                                       CATALOG_TO_TAGS, MANAGED_TAGS)
from app.services.permission_catalog import default_role_map, position_role
from werkzeug.security import generate_password_hash
from flask import session as _sess

FAILS = []
def chk(n, c, extra=""):
    print(("PASS " if c else "FAIL "), n, ("| " + str(extra)) if extra else "")
    if not c:
        FAILS.append(n)

print("A boot: create_app() succeeded")

# ------------------------------------------------------------------
# Static preconditions (the facts the live checks below depend on).
# ------------------------------------------------------------------
chk("pre: MANAGED_TAGS == the exact 4 catalog-controlled route tags",
    MANAGED_TAGS == {"drivers.reset_passcode", "legal.upload_document",
                     "developer.view_chat", "legal.view_insurance"},
    sorted(MANAGED_TAGS))
chk("pre: dash.dev_chat -> developer.view_chat is wired in CATALOG_TO_TAGS",
    CATALOG_TO_TAGS.get("dash.dev_chat") == {"developer.view_chat"},
    CATALOG_TO_TAGS.get("dash.dev_chat"))

corp_role = ROLE_PERMISSIONS.get("corporate", set())
gm_role = ROLE_PERMISSIONS.get("gm", set())
drm = default_role_map()
corp_cat_def = drm.get("corporate", set())

chk("pre: corporate role baseline HAS developer.view_chat (so OFF must revoke it)",
    "developer.view_chat" in corp_role)
chk("pre: developer.view_chat IS managed (in MANAGED_TAGS)",
    "developer.view_chat" in MANAGED_TAGS)
chk("pre: dash.dev_chat is NOT a corporate catalog-default (so a saved OFF config omits it)",
    "dash.dev_chat" not in corp_cat_def, sorted(corp_cat_def))
chk("pre: corporate role HAS team_reports.view AND it is NOT managed (pass-through)",
    "team_reports.view" in corp_role and "team_reports.view" not in MANAGED_TAGS)
chk("pre: position 'Corporate' maps to role key 'corporate'",
    position_role("Corporate") == "corporate", position_role("Corporate"))
chk("pre: gm role baseline HAS drivers.reset_passcode (a MANAGED tag) for the belt test",
    "drivers.reset_passcode" in gm_role)

# A catalog key that IS a corporate catalog-default + is NOT dash.dev_chat - used
# to make the saved PositionPermission config truthy (so the per-position branch
# uses the SAVED set, NOT the catalog default) while still OMitting dash.dev_chat.
FILLER_ON = "reports.labor"   # corporate catalog-default, not dash.dev_chat
chk("pre: filler key reports.labor IS a corporate catalog-default (makes saved-config truthy)",
    FILLER_ON in corp_cat_def, FILLER_ON)

# ------------------------------------------------------------------
# Seed.
# ------------------------------------------------------------------
db = SessionLocal()
for M in (PositionPermission, EmployeePosition, Employee, User):
    db.query(M).delete()
db.commit()

corp_pid = db.query(Position).filter(Position.name == "Corporate").first().id
gm_pid = db.query(Position).filter(Position.name == "GM").first().id

# OFF-REVOKE user: corporate, linked Employee, positioned 'Corporate' @ tomball.
u_corp = User(full_name="Corp", email="corp@x.com", phone=None,
              passcode_hash=generate_password_hash("12345"),
              permission_level="corporate", store_scope="tomball,copperfield",
              first_login_done=True, active=True, session_version=0)
db.add(u_corp); db.flush()
e_corp = Employee(full_name="Corp Emp", email="corp@x.com", active=True,
                  user_id=u_corp.id)
db.add(e_corp); db.flush()
db.add(EmployeePosition(employee_id=e_corp.id, position_id=corp_pid,
                        store_key="tomball"))
# Saved PositionPermission config for (corporate, tomball): one ON filler so the
# saved config is non-empty (the per-position branch then uses SAVED, bypassing
# the catalog default), and dash.dev_chat is DELIBERATELY NOT seeded -> OFF.
db.add(PositionPermission(position_key="corporate", store_key="tomball",
                          perm_key=FILLER_ON))
db.commit()
corp_uid = u_corp.id

# BELT user: a gm-linked Employee with NO positions anywhere.
u_gm = User(full_name="GM", email="gm@x.com", phone=None,
            passcode_hash=generate_password_hash("12345"),
            permission_level="gm", store_scope="tomball,copperfield",
            first_login_done=True, active=True, session_version=0)
db.add(u_gm); db.flush()
e_gm = Employee(full_name="GM Emp", email="gm@x.com", active=True, user_id=u_gm.id)
db.add(e_gm); db.flush()
gm_uid = u_gm.id

# PARTNER user: no positions at all.
u_par = User(full_name="Partner", email="par@x.com", phone=None,
             passcode_hash=generate_password_hash("12345"),
             permission_level="partner", store_scope=None,
             first_login_done=True, active=True, session_version=0)
db.add(u_par); db.commit()
par_uid = u_par.id

U_CORP = db.get(User, corp_uid)
U_GM = db.get(User, gm_uid)
U_PAR = db.get(User, par_uid)
db.close()

# ------------------------------------------------------------------
# OFF-REVOKE: the core new capability.
# ------------------------------------------------------------------
# 1) dash.dev_chat OFF (saved config exists but omits it) -> developer.view_chat REVOKED.
with app.test_request_context("/"):
    _sess["active_store"] = "tomball"
    off = _user_has(U_CORP, "developer.view_chat")
chk("OFF-REVOKE: corporate @ tomball, dash.dev_chat OFF -> developer.view_chat REVOKED (False)",
    off is False, off)

# 2) Now toggle dash.dev_chat ON for (corporate, tomball) -> developer.view_chat GRANTED.
db = SessionLocal()
db.add(PositionPermission(position_key="corporate", store_key="tomball",
                          perm_key="dash.dev_chat"))
db.commit(); db.close()
with app.test_request_context("/"):
    _sess["active_store"] = "tomball"
    on = _user_has(U_CORP, "developer.view_chat")
chk("OFF-REVOKE: dash.dev_chat ON for (corporate,tomball) -> developer.view_chat GRANTED (True)",
    on is True, on)

# ------------------------------------------------------------------
# UNMAPPED PASS-THROUGH: a non-managed role tag stays on the baseline.
# (Same positioned corporate user; team_reports.view is NOT managed and is never
# seeded in the catalog config, yet the role baseline must still grant it.)
# ------------------------------------------------------------------
with app.test_request_context("/"):
    _sess["active_store"] = "tomball"
    passthru = _user_has(U_CORP, "team_reports.view")
chk("PASS-THROUGH: team_reports.view (NOT managed) stays role-baseline TRUE for positioned corp",
    passthru is True, passthru)

# Belt-and-suspenders: the OTHER managed tags the catalog never granted for this
# corporate position are revoked too (corporate role does NOT carry them, so they
# are simply absent - no lockout, just not spuriously present).
with app.test_request_context("/"):
    _sess["active_store"] = "tomball"
    other_managed = _user_has(U_CORP, "legal.upload_document")
chk("PASS-THROUGH ctrl: legal.upload_document (managed, not in corp role, not granted) -> False",
    other_managed is False, other_managed)

# ------------------------------------------------------------------
# BELT: no positions -> FULL role baseline, a managed tag is NOT wrongly revoked.
# ------------------------------------------------------------------
# active_store=tomball but the gm has NO positions there -> belt returns role_perms.
with app.test_request_context("/"):
    _sess["active_store"] = "tomball"
    belt_managed_store = _user_has(U_GM, "drivers.reset_passcode")
chk("BELT a: gm no-positions @ tomball -> MANAGED tag drivers.reset_passcode NOT revoked (True)",
    belt_managed_store is True, belt_managed_store)

# active_store=None -> belt returns role_perms.
with app.test_request_context("/"):
    _sess.pop("active_store", None)
    belt_managed_none = _user_has(U_GM, "drivers.reset_passcode")
chk("BELT b: gm active_store=None -> MANAGED tag drivers.reset_passcode NOT revoked (True)",
    belt_managed_none is True, belt_managed_none)

# And an unmanaged role tag is fine on the belt too.
with app.test_request_context("/"):
    _sess.pop("active_store", None)
    belt_unmanaged = _user_has(U_GM, "team_reports.view")
chk("BELT c: gm active_store=None -> unmanaged role tag team_reports.view granted (True)",
    belt_unmanaged is True, belt_unmanaged)

# ------------------------------------------------------------------
# PARTNER: wildcard TRUE for any tag (managed or not, with no positions).
# ------------------------------------------------------------------
with app.test_request_context("/"):
    _sess.pop("active_store", None)
    p1 = _user_has(U_PAR, "developer.view_chat")   # a MANAGED tag
    p2 = _user_has(U_PAR, "some.tag.that.does.not.exist")
chk("PARTNER: wildcard GRANTS a MANAGED tag with no positions (developer.view_chat)", p1 is True, p1)
chk("PARTNER: wildcard GRANTS a nonexistent tag", p2 is True, p2)

print()
print("=== PIECE A (catalog-authoritative / OFF-revoke) GATE:",
      "ALL PASS" if not FAILS else "%d FAIL %s" % (len(FAILS), FAILS), "===")
