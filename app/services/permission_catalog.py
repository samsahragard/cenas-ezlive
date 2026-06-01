"""Cenas PERMISSIONS-page catalog (aick backend, Sam #1676).

100% the Cenas platform - NO Toast/external wiring. Each permission links to the
real app area it gates (maps_to). Drives BOTH the page UI (ck, consumes JSON) and
enforcement (aick, wires keys into permissions.py.requires_permission).

SEPARATE from the LOCKED ezCater-driver perms - those are coded-fixed, untouched.

CONTRACT (per ck #1679 + aick #1680):
  per-permission: id, key, category{num,name}, label, notes, maps_to{route, blueprint_or_fn},
                  status: 'live'|'reserved', default_roles[]
  top-level: ROLES[]{key,label,wildcard}, STORES[]{key,label}
  save payload (frontend->backend): {user_id, stores[], role,
      overrides:{<store_key>:{<perm_key>: allow|deny|inherit}}}   # PER-STORE (ck #1686)
  Each perm also carries sensitive:bool (authoritative - drives the lock-icon UX).

maps_to marked 'verify:' = best-effort route pending a grep-pass against app/web.
status 'reserved' = toggle persists now, enforces once that surface ships.
default_roles = which role templates inherit=ON by default (Sam tunes per-user via overrides).
"""

# ---- Roles (Sam #1681 + #2378/#2381: the 14 canonical scheduling positions +
# DRIVER (corporate_driver) + EXPO; prep_manager dropped; 'host' shown as Hostess,
# corporate_driver shown as Driver. The ezCater 'driver' is LOCKED/separate -
# not a template here. ----
ROLES = [
    {"key": "partner",         "label": "Partner",                  "wildcard": True},
    {"key": "corporate",       "label": "Corporate",                "wildcard": False},
    {"key": "corporate_chef",  "label": "Corporate Chef",           "wildcard": False},
    {"key": "gm",              "label": "General Manager",          "wildcard": False},
    {"key": "km",              "label": "Kitchen Manager",          "wildcard": False},
    {"key": "assistant_km",    "label": "Assistant Kitchen Manager","wildcard": False},
    {"key": "foh_manager",     "label": "FOH Manager",              "wildcard": False},
    {"key": "expo",            "label": "Expo",                     "wildcard": False},
    {"key": "cashier",         "label": "Cashier",                  "wildcard": False},
    {"key": "server",          "label": "Server",                   "wildcard": False},
    {"key": "bartender",       "label": "Bartender",                "wildcard": False},
    {"key": "well",            "label": "Well",                     "wildcard": False},
    {"key": "busser",          "label": "Busser",                   "wildcard": False},
    {"key": "host",            "label": "Hostess",                  "wildcard": False},
    {"key": "cook",            "label": "Cook",                     "wildcard": False},
    {"key": "corporate_driver","label": "Driver",                   "wildcard": False},
]

STORES = [
    {"key": "copperfield", "label": "Cenas Kitchen - Copperfield"},
    {"key": "tomball",     "label": "Cenas Kitchen - Tomball"},
]

# ---- default_roles helper sets (interpret Sam's level-notes). partner has wildcard so
# is implicitly granted everything; listed explicitly for clarity. Sam overrides per-user. ----
ALL_ROLES   = [r["key"] for r in ROLES]
PARTNER     = ["partner"]
CORP_UP     = ["partner", "corporate", "corporate_chef"]                       # corporate tier+
GM_UP       = ["partner", "corporate", "corporate_chef", "gm"]                 # GM-and-above
MGR_UP      = ["partner", "corporate", "corporate_chef", "gm", "km",
               "assistant_km", "foh_manager"]                                  # manager-level+
KITCHEN_MGR = ["partner", "corporate_chef", "km"]                              # KM + chefs
KITCHEN     = ["partner", "corporate_chef", "km", "cook", "expo"]              # kitchen staff
DRIVERS_MGR = ["partner", "corporate", "gm", "corporate_driver"]              # drivers + mgrs

# ---- ADD-PEOPLE rank tiers (Sam #2381/#2383): a manager can ADD only roles
# STRICTLY BELOW their own rank. Partner adds anyone; GM/KM (+ corporate_chef)
# add FOH-Mgr/Asst-KM + everything below; Asst-KM/FOH-Mgr add only the floor
# below them; floor + access-only roles add no one. corporate -> just below
# partner; corporate_chef -> KM tier (Sam to confirm, #2391). The +Add position
# list + the permissions UI gate off addable_roles(). ----
ROLE_RANK = {
    "partner": 100,
    "corporate": 90,
    "corporate_chef": 70,
    "gm": 70, "km": 70,
    "assistant_km": 50, "foh_manager": 50,
    "expo": 10, "cook": 10, "server": 10, "bartender": 10,
    "well": 10, "busser": 10, "host": 10, "cashier": 10,
    "corporate_driver": 10,
}

def addable_roles(actor_role):
    """Role keys an actor of `actor_role` may ADD. PARTNER (the top-rank wildcard
    owner) adds EVERY role incl peers - Sam #2381 'all permission sets'; everyone
    else adds STRICTLY BELOW their own rank (a GM can't add a KM; peers can't add
    peers). Unknown/None actor -> no add rights."""
    actor_rank = ROLE_RANK.get((actor_role or "").strip().lower())
    if actor_rank is None:
        return set()
    if actor_rank >= max(ROLE_RANK.values()):    # partner -> adds anyone
        return set(ROLE_RANK)
    return {k for k, r in ROLE_RANK.items() if r < actor_rank}


# Canonical scheduling-position NAME -> permissions ROLE key, for the +Add
# rank-gate (the 14 positions map to their role; expo/driver are access-only,
# not scheduling positions, so they aren't here). 'Well' -> well, 'Hostess' -> host.
POSITION_TO_ROLE = {
    "partner": "partner", "corporate": "corporate", "corporate chef": "corporate_chef",
    "gm": "gm", "km": "km", "assistant km": "assistant_km", "foh manager": "foh_manager",
    "busser": "busser", "hostess": "host", "cashier": "cashier", "server": "server",
    "well": "well", "bartender": "bartender", "cook": "cook",
}

def position_role(position_name):
    """The permissions ROLE key for a canonical scheduling-position name (drives
    the +Add rank-gate). Unknown name -> None (the gate skips it)."""
    return POSITION_TO_ROLE.get((position_name or "").strip().lower())

def _c(num, name): return {"num": num, "name": name}

# ---- The catalog. 14 categories, renumbered clean (Sam's 14-18 gap closed). ----
CATALOG = [
 {"id": 1, "key": "dashboard", "name": "Dashboard Access", "perms": [
   {"id":"1.1","key":"dash.today","label":"Access Today Dashboard","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:/<store>/today","blueprint_or_fn":"store_routes (today tab)"},
    "notes":"Shows the Today tab - daily overview home for partners/managers."},
   {"id":"1.2","key":"dash.manager","label":"Access Manager Dashboard","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/<store>/manager","blueprint_or_fn":"store_routes.py:3202"},
    "notes":"Shows the Manager tab (Daily Log, Incidents, Attendance, Training...). Required to run a shift."},
   {"id":"1.3","key":"dash.catering","label":"Access Catering Dashboard","status":"live","default_roles":MGR_UP+["corporate_driver"],
    "maps_to":{"route":"verify:ez/catering","blueprint_or_fn":"ezcater_routes / ezcater_live_routes"},
    "notes":"Shows the Catering tab (EZ orders queue, driver assignment, tracking). Drivers + catering mgrs."},
   {"id":"1.4","key":"dash.operations","label":"Access Operations Dashboard","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"verify:/<store>/operations","blueprint_or_fn":"store_routes (operations tab)"},
    "notes":"Shows Operations tab (Team, Forecasts, Sales, Labor, Marketing). Partner/GM-only typically."},
   {"id":"1.5","key":"dash.vendors","label":"Access Vendors Dashboard","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"verify:/<store>/vendors","blueprint_or_fn":"vendors/produce routes"},
    "notes":"Shows Vendors tab (directory, POs, invoices, portal). GM-and-above."},
   {"id":"1.6","key":"dash.kitchen","label":"Access Kitchen Dashboard","status":"live","default_roles":KITCHEN,
    "maps_to":{"route":"verify:/<store>/kitchen","blueprint_or_fn":"store_routes (kitchen: fresh/prep/recipes)"},
    "notes":"Shows Kitchen tab (Fresh Food, Prep List, Recipes). KMs, chefs, prep cooks."},
   {"id":"1.7","key":"dash.legal","label":"Access Legal Dashboard","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:legal (SOON)","blueprint_or_fn":"legal_routes"},
    "notes":"Shows Legal tab (licenses, permits, insurance, contracts). Partner-only. Mostly SOON."},
   {"id":"1.8","key":"dash.cena_chat","label":"Access Partner Chat (Cena)","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"/sam/chat","blueprint_or_fn":"sam_chat.py (SAM_CHAT_USER_ID single-user gate today)"},
    "notes":"Open /sam/chat + talk to Cena (operator AI). Currently single-user gated."},
   {"id":"1.9","key":"dash.dev_chat","label":"Access Dev Chat","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"/partner/developer/chat","blueprint_or_fn":"developer_chat.py (developer.view_chat)"},
    "notes":"See the engineering coordination chat. Partner-only. Confidential."},
 ]},
 {"id": 2, "key": "time", "name": "Time & Attendance", "perms": [
   {"id":"2.1","key":"time.view_own","label":"View Own Time Entries","status":"reserved","default_roles":ALL_ROLES,
    "maps_to":{"route":"verify:schedules-v2","blueprint_or_fn":"Schedules V2 (not yet shipped)"},
    "notes":"See own clock-in/out history + hours. Default for all employees."},
   {"id":"2.2","key":"time.view_all","label":"View All Time Entries","status":"reserved","default_roles":MGR_UP,
    "maps_to":{"route":"verify:schedules-v2","blueprint_or_fn":"Schedules V2"},
    "notes":"See everyone's time entries (store-scoped). Manager-level."},
   {"id":"2.3","key":"time.edit_others","label":"Edit Other Employees' Time Entries","status":"reserved","default_roles":MGR_UP,
    "maps_to":{"route":"verify:schedules-v2","blueprint_or_fn":"Schedules V2 (audit-logged)"},
    "notes":"Fix anyone's hours (forgotten clock-out etc). Manager-level. Audit-logged."},
   {"id":"2.4","key":"attendance.view","label":"View Attendance Records","status":"reserved","default_roles":MGR_UP,
    "maps_to":{"route":"verify:schedules-v2","blueprint_or_fn":"Schedules V2"},
    "notes":"See attendance patterns (late, no-show, sick). Manager-level."},
   {"id":"2.5","key":"attendance.edit","label":"Edit Attendance Records","status":"reserved","default_roles":MGR_UP,
    "maps_to":{"route":"verify:schedules-v2","blueprint_or_fn":"Schedules V2"},
    "notes":"Excuse absence, change status, add notes. Manager-level."},
   {"id":"2.6","key":"schedule.configure","label":"Configure Schedules","status":"reserved","default_roles":MGR_UP,
    "maps_to":{"route":"verify:/<store>/schedule (Schedules V2)","blueprint_or_fn":"Schedules V2"},
    "notes":"Build/edit weekly schedule, assign shifts, time-off. GM-and-above."},
   {"id":"2.7","key":"schedule.view","label":"View Schedule","status":"reserved","default_roles":ALL_ROLES,
    "maps_to":{"route":"verify:/<store>/schedule","blueprint_or_fn":"Toast-shift-sourced today; Schedules V2"},
    "notes":"See published schedule. Default for all employees."},
   {"id":"2.8","key":"timeoff.request","label":"Request Time Off","status":"reserved","default_roles":ALL_ROLES,
    "maps_to":{"route":"verify:schedules-v2","blueprint_or_fn":"Schedules V2"},
    "notes":"Submit time-off requests."},
   {"id":"2.9","key":"timeoff.approve","label":"Approve Time Off Requests","status":"reserved","default_roles":MGR_UP,
    "maps_to":{"route":"verify:schedules-v2","blueprint_or_fn":"Schedules V2"},
    "notes":"Approve/reject time-off. Manager-and-above."},
   {"id":"2.10","key":"availability.manage","label":"Set & Manage Availability","status":"reserved","default_roles":MGR_UP,
    "maps_to":{"route":"verify:/<store>/schedules-v2/employees/<id>/availability","blueprint_or_fn":"Schedules V2 roster (manager-set availability)"},
    "notes":"Set, adjust, change or delete an employee's availability (in the team roster). Manager-level -- whoever can configure schedules. Employees no longer self-set their own."},
 ]},
 {"id": 3, "key": "catering", "name": "Catering & EZ Orders", "perms": [
   {"id":"3.1","key":"catering.view","label":"View Catering Orders","status":"live","default_roles":MGR_UP+["corporate_driver"],
    "maps_to":{"route":"verify:/ez (orders queue)","blueprint_or_fn":"ezcater_routes.py"},
    "notes":"See EZ orders queue (pending/in-progress/completed). Anyone working catering."},
   {"id":"3.2","key":"catering.edit","label":"Edit Catering Order Details","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:/ez/<id>","blueprint_or_fn":"ezcater_routes.py"},
    "notes":"Modify delivery time, contact, instructions, items. Manager-level."},
   {"id":"3.3","key":"catering.assign_driver","label":"Assign Drivers to Catering Orders","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:/ez driver-assign POST","blueprint_or_fn":"ezcater_routes -> pwck Playwright swap"},
    "notes":"Driver-assign dropdown; triggers the Playwright ezCater-driver swap. Manager-level."},
   {"id":"3.4","key":"catering.unassign","label":"Unassign Drivers","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:/ez unassign","blueprint_or_fn":"ezcater_routes (requires_store_access)"},
    "notes":"Remove a driver from an order; reverses via Playwright. Manager-level."},
   {"id":"3.5","key":"catering.view_drivers","label":"View Driver List","status":"live","default_roles":MGR_UP+["corporate_driver"],
    "maps_to":{"route":"verify:/ez drivers","blueprint_or_fn":"ezcater_routes / drivers.view_roster"},
    "notes":"See available drivers + assignments. Drivers (own queue) + managers (assign)."},
   {"id":"3.6","key":"catering.revenue","label":"View Catering Revenue Reports","status":"live","default_roles":CORP_UP,
    "maps_to":{"route":"verify:catering reports","blueprint_or_fn":"ezcater_revenue.py"},
    "notes":"Revenue, avg order size, top customers. Partner-and-above."},
   {"id":"3.7","key":"catering.print_pdf","label":"Print/Download Catering PDFs","status":"live","default_roles":MGR_UP+["corporate_driver"],
    "maps_to":{"route":"verify:/ez/<id>/pdf","blueprint_or_fn":"ezcater_routes (PDF)"},
    "notes":"Download EZ order PDFs for routing/records."},
   {"id":"3.8","key":"catering.reassign_store","label":"Reassign Order Between Stores","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:/ez reassign","blueprint_or_fn":"ezcater_routes (both-store scope)"},
    "notes":"Move an order Tomball<->Copperfield. Manager-level, needs both-store scope."},
   {"id":"3.9","key":"catering.driver_perf","label":"View Driver Performance","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:driver perf","blueprint_or_fn":"ezcater_payroll / metrics"},
    "notes":"Per-driver on-time rate, feedback."},
 ]},
 {"id": 4, "key": "manager", "name": "Manager Powers", "perms": [
   {"id":"4.1","key":"manager_log.write","label":"Daily Log Entry","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/<store>/manager/daily-log","blueprint_or_fn":"store_routes (manager_log.write)"},
    "notes":"Write Manager Daily Log entries. Required for managers running a shift."},
   {"id":"4.2","key":"incident.create","label":"Create Incident Reports","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/<store>/manager/incident-reports","blueprint_or_fn":"store_routes (IncidentReport)"},
    "notes":"File incident reports (complaints, employee issues, accidents). Manager-level."},
   {"id":"4.3","key":"incident.view","label":"View Incident Reports","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/<store>/manager/incident-reports","blueprint_or_fn":"store_routes"},
    "notes":"Read incidents filed by others. Manager-and-above."},
   {"id":"4.4","key":"incident.edit","label":"Edit Incident Reports","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"/<store>/manager/incident-reports/<id>/edit","blueprint_or_fn":"store_routes"},
    "notes":"Update/close an incident. GM-and-above (should be near-immutable after filing)."},
   {"id":"4.5","key":"team.notify","label":"Send Team Notifications","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:notify","blueprint_or_fn":"Telegram bridge / push"},
    "notes":"Push announcements to team phones. Manager-and-above."},
   {"id":"4.6","key":"team.moderate_chat","label":"Moderate Team Chat","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:team-chat","blueprint_or_fn":"team chat (reserved)"},
    "notes":"Delete messages, mute users, manage channels. GM-and-above."},
 ]},
 {"id": 5, "key": "vendors", "name": "Vendors & Purchasing", "perms": [
   {"id":"5.1","key":"vendors.view","label":"View Vendors List","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:/<store>/vendors","blueprint_or_fn":"vendors routes (produce.view_vendor_list)"},
    "notes":"See vendor directory + contacts. Manager-and-above."},
   {"id":"5.2","key":"vendors.add","label":"Add New Vendors","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"verify:/<store>/vendors new","blueprint_or_fn":"vendors routes"},
    "notes":"Create vendor records. GM-and-above."},
   {"id":"5.3","key":"vendors.edit","label":"Edit Vendor Info","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"verify:/<store>/vendors/<id>","blueprint_or_fn":"vendors routes"},
    "notes":"Update contacts, terms, account numbers. GM-and-above."},
   {"id":"5.4","key":"vendors.view_invoices","label":"View Vendor Invoices","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:invoices","blueprint_or_fn":"produce.upload_invoice / AP"},
    "notes":"See accounts payable - what's owed. Manager-and-above."},
   {"id":"5.5","key":"vendors.mark_paid","label":"Mark Invoices as Paid","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:invoices mark-paid","blueprint_or_fn":"AP (reserved)"},
    "notes":"Set invoice status paid after payment. GM-and-above."},
   {"id":"5.6","key":"vendors.pay","label":"Pay Vendor Invoices","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:pay","blueprint_or_fn":"payment (reserved, not wired)"},
    "notes":"Trigger actual payment. Partner-only."},
   {"id":"5.7","key":"vendors.spend_reports","label":"View Vendor Spend Reports","status":"reserved","default_roles":CORP_UP,
    "maps_to":{"route":"verify:spend reports","blueprint_or_fn":"reporting (reserved)"},
    "notes":"Spend by vendor/category over time. Partner-and-above."},
 ]},
 {"id": 6, "key": "kitchen", "name": "Kitchen Operations", "perms": [
   {"id":"6.1","key":"kitchen.fresh_view","label":"View Fresh Food List","status":"live","default_roles":KITCHEN,
    "maps_to":{"route":"verify:/<store>/kitchen/fresh","blueprint_or_fn":"FreshFood (ck)"},
    "notes":"See day's fresh-food items. Required for kitchen staff."},
   {"id":"6.2","key":"kitchen.fresh_edit","label":"Edit Fresh Food List","status":"live","default_roles":KITCHEN_MGR,
    "maps_to":{"route":"verify:/<store>/kitchen/fresh edit","blueprint_or_fn":"FreshFood"},
    "notes":"Add/remove, update quantities, mark ready. KM + chefs."},
   {"id":"6.3","key":"kitchen.prep_view","label":"View Prep List","status":"live","default_roles":KITCHEN,
    "maps_to":{"route":"verify:/<store>/kitchen/prep","blueprint_or_fn":"PrepList"},
    "notes":"See prep tasks. Required for prep cooks."},
   {"id":"6.4","key":"kitchen.prep_edit","label":"Edit Prep List","status":"live","default_roles":KITCHEN_MGR+["cook"],
    "maps_to":{"route":"verify:/<store>/kitchen/prep edit","blueprint_or_fn":"PrepList"},
    "notes":"Mark complete, add tasks. KM + prep cooks."},
   {"id":"6.5","key":"kitchen.recipes_view","label":"View Recipes","status":"live","default_roles":KITCHEN,
    "maps_to":{"route":"/<store>/recipes","blueprint_or_fn":"store_routes.py:3480 (recipes_index)"},
    "notes":"Read the recipe book. Required for cooks."},
   {"id":"6.6","key":"kitchen.inventory","label":"Update Inventory Counts","status":"reserved","default_roles":KITCHEN_MGR,
    "maps_to":{"route":"verify:inventory","blueprint_or_fn":"inventory (reserved)"},
    "notes":"Adjust on-hand quantities. KM-and-above."},
 ]},
 {"id": 7, "key": "employees", "name": "Employee Management", "perms": [
   {"id":"7.1","key":"emp.view_directory","label":"View Employee Directory","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/partner/team","blueprint_or_fn":"team_routes.py (access.team_admin / Team UI)"},
    "notes":"Team list with names/roles. Manager-and-above."},
   {"id":"7.2","key":"emp.add","label":"Add New Employees","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/partner/team add","blueprint_or_fn":"team_routes.py"},
    "notes":"Create user accounts. GM-and-above."},
   {"id":"7.3","key":"emp.edit_info","label":"Edit Employee Info","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"/partner/team/<id>","blueprint_or_fn":"team_routes.py"},
    "notes":"Update contact, address, emergency contact, role. GM-and-above."},
   {"id":"7.4","key":"emp.view_wages","label":"View Employee Wages","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"verify:team wages","blueprint_or_fn":"team / labor (very sensitive)"},
    "notes":"See individual pay rates. Partner-only / payroll-handler. Very sensitive."},
   {"id":"7.5","key":"emp.edit_wages","label":"Edit Employee Wages","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"verify:team wages edit","blueprint_or_fn":"team (audit-logged)"},
    "notes":"Change pay rate. Partner-only. Audit-logged."},
   {"id":"7.6","key":"emp.archive","label":"Archive/Deactivate Employee","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"/partner/team/<id> deactivate","blueprint_or_fn":"team_routes.py"},
    "notes":"Mark no-longer-working; removes access, keeps history. GM-and-above."},
   {"id":"7.7","key":"emp.reset_passcode","label":"Reset Employee Passcode","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"team reset / /<store>/drivers reset","blueprint_or_fn":"team_routes.py:268 (drivers.reset_passcode)"},
    "notes":"Generate a new PIN. Manager-and-above."},
   {"id":"7.8","key":"emp.view_tax_masked","label":"View Tax Identifiers (Masked)","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:tax-id","blueprint_or_fn":"reserved (not built)"},
    "notes":"Partial SSN (last 4). GM-and-above."},
   {"id":"7.9","key":"emp.view_tax_full","label":"View Tax Identifiers (Full)","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:tax-id full","blueprint_or_fn":"reserved (audit-logged)"},
    "notes":"Full SSN. Partner-only. Strictly audit-logged."},
   {"id":"7.10","key":"emp.edit_tax","label":"Edit Tax Identifiers","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:tax-id edit","blueprint_or_fn":"reserved (audit-logged)"},
    "notes":"Add/correct SSN. Partner-only. Audit-logged."},
   {"id":"7.11","key":"emp.view_dd","label":"View Direct Deposit Info","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:direct-deposit","blueprint_or_fn":"reserved"},
    "notes":"Bank account for payroll. Partner-only."},
   {"id":"7.12","key":"emp.edit_dd","label":"Edit Direct Deposit Info","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:direct-deposit edit","blueprint_or_fn":"reserved (audit-logged)"},
    "notes":"Change payroll bank account. Partner-only. Audit-logged."},
   {"id":"7.13","key":"emp.view_onboarding","label":"View Onboarding Documents","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:onboarding","blueprint_or_fn":"reserved"},
    "notes":"I-9, W-4, signed agreements. GM-and-above."},
   {"id":"7.14","key":"emp.upload_onboarding","label":"Upload Onboarding Documents","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:onboarding upload","blueprint_or_fn":"reserved"},
    "notes":"Add docs to employee file. GM-and-above."},
   {"id":"7.15","key":"emp.view_perf","label":"View Employee Performance Notes","status":"reserved","default_roles":MGR_UP,
    "maps_to":{"route":"verify:counseling","blueprint_or_fn":"manager/counseling (EmployeeCounseling)"},
    "notes":"Manager notes on performance. Manager-and-above."},
   {"id":"7.16","key":"emp.edit_perf","label":"Edit Employee Performance Notes","status":"reserved","default_roles":MGR_UP,
    "maps_to":{"route":"verify:counseling edit","blueprint_or_fn":"manager/counseling"},
    "notes":"Add/update performance notes. Manager-and-above."},
 ]},
 {"id": 8, "key": "training", "name": "Training & Certifications", "perms": [
   {"id":"8.1","key":"training.view_own","label":"View Own Training Records","status":"live","default_roles":ALL_ROLES,
    "maps_to":{"route":"/<store>/manager/training","blueprint_or_fn":"store_routes (TrainingRecord)"},
    "notes":"Own certs + completion. Default for all employees."},
   {"id":"8.2","key":"training.view_all","label":"View All Training Records","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/<store>/manager/training","blueprint_or_fn":"store_routes"},
    "notes":"Team cert status. Manager-and-above."},
   {"id":"8.3","key":"training.view_expiring","label":"View Expiring/Overdue Certs","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"/<store>/manager/training (expiring)","blueprint_or_fn":"store_routes"},
    "notes":"Certs expiring/expired. GM-and-above."},
   {"id":"8.4","key":"training.mark_complete","label":"Mark Training Complete","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/<store>/manager/training mark","blueprint_or_fn":"store_routes"},
    "notes":"Sign off completion. Manager-and-above."},
   {"id":"8.5","key":"training.edit","label":"Add/Edit Training Records","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"/<store>/manager/training edit","blueprint_or_fn":"store_routes"},
    "notes":"Create/update training entries. GM-and-above."},
   {"id":"8.6","key":"training.upload","label":"Upload Certification Documents","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"/<store>/manager/training upload","blueprint_or_fn":"store_routes"},
    "notes":"Attach proof (food handler, TABC). GM-and-above."},
   {"id":"8.7","key":"training.configure","label":"Configure Training Requirements","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:training config","blueprint_or_fn":"reserved"},
    "notes":"Set required certs per role. Partner-only."},
   {"id":"8.8","key":"training.remind","label":"Send Training Reminders","status":"reserved","default_roles":MGR_UP,
    "maps_to":{"route":"verify:training remind","blueprint_or_fn":"reserved"},
    "notes":"Notify staff of upcoming required training. Manager-and-above."},
 ]},
 {"id": 9, "key": "maintenance", "name": "Maintenance & Equipment", "perms": [
   {"id":"9.1","key":"maint.view","label":"View Maintenance Requests","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/<store>/manager/maintenance","blueprint_or_fn":"store_routes (MaintenanceRequest)"},
    "notes":"See open maintenance issues. Manager-and-above."},
   {"id":"9.2","key":"maint.submit","label":"Submit Maintenance Request","status":"live","default_roles":ALL_ROLES,
    "maps_to":{"route":"/<store>/manager/maintenance new","blueprint_or_fn":"store_routes"},
    "notes":"Flag a problem (broken oven etc). Default for all staff."},
   {"id":"9.3","key":"maint.edit","label":"Edit Maintenance Request","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/<store>/manager/maintenance/<id>","blueprint_or_fn":"store_routes"},
    "notes":"Update status, notes, priority. Manager-and-above."},
   {"id":"9.4","key":"maint.close","label":"Close Maintenance Request","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"/<store>/manager/maintenance/<id> close","blueprint_or_fn":"store_routes"},
    "notes":"Mark resolved. Manager-and-above."},
   {"id":"9.5","key":"maint.approve_spend","label":"Approve Maintenance Spend","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:maint approve","blueprint_or_fn":"reserved (threshold)"},
    "notes":"Sign off repair costs above a threshold. GM-and-above."},
   {"id":"9.6","key":"equip.view","label":"View Equipment Records","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:/<store>/manager/maintenance (equipment cards)","blueprint_or_fn":"maintenance page (warranty cols, aick)"},
    "notes":"Equipment inventory (ovens, fridges, fryers, POS hw). Manager-and-above."},
   {"id":"9.7","key":"equip.add","label":"Add Equipment Records","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"verify:equipment add","blueprint_or_fn":"maintenance page"},
    "notes":"Add equipment to the tracker. GM-and-above."},
   {"id":"9.8","key":"equip.edit","label":"Edit Equipment Records","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"verify:equipment edit","blueprint_or_fn":"maintenance page"},
    "notes":"Update serial, location, condition. GM-and-above."},
   {"id":"9.9","key":"equip.view_warranty","label":"View Warranty Information","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:equipment warranty","blueprint_or_fn":"maintenance page (KitchenReady/Safeware cols)"},
    "notes":"Warranty status, expiration, provider. Manager-and-above."},
 ]},
 {"id": 10, "key": "reporting", "name": "Reporting", "perms": [
   {"id":"10.1","key":"reports.sales","label":"View Sales Reports","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:sales reports","blueprint_or_fn":"toast_reports / sales.view_* (Toast-derived)"},
    "notes":"Revenue by day/week/month/station/category. Manager-and-above."},
   {"id":"10.2","key":"reports.labor","label":"View Labor Reports","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"verify:labor reports","blueprint_or_fn":"toast_reports.labor_report (labor.view_*)"},
    "notes":"Hours, labor cost, labor % of sales. GM-and-above."},
   {"id":"10.3","key":"reports.menu","label":"View Menu Performance Reports","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:menu reports","blueprint_or_fn":"reserved"},
    "notes":"Item sell-through by daypart. GM-and-above."},
   {"id":"10.4","key":"reports.catering","label":"View Catering Reports","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:catering reports","blueprint_or_fn":"ezcater_revenue.py"},
    "notes":"Catering revenue, patterns, top customers. Manager-and-above."},
   {"id":"10.5","key":"reports.forecasts","label":"View Forecasts","status":"reserved","default_roles":CORP_UP,
    "maps_to":{"route":"verify:forecasts (SOON)","blueprint_or_fn":"reserved (labeled SOON)"},
    "notes":"Predicted sales/labor/busy periods. Partner-and-above when launched."},
   {"id":"10.6","key":"reports.marketing","label":"View Marketing Reports","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:marketing","blueprint_or_fn":"reserved"},
    "notes":"Loyalty signups, promo redemption, online trends. GM-and-above."},
   {"id":"10.7","key":"reports.giftcard","label":"View Gift Card / Rewards Reports","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:giftcard","blueprint_or_fn":"reserved"},
    "notes":"Gift card liability, rewards redemption. GM-and-above."},
   {"id":"10.8","key":"reports.cross_store","label":"View Cross-Store Reports","status":"live","default_roles":CORP_UP,
    "maps_to":{"route":"verify:cross-store","blueprint_or_fn":"team_reports.view_all_stores / sales.view_all_stores"},
    "notes":"Combined Tomball+Copperfield. Partner-only / all-stores scope."},
   {"id":"10.9","key":"reports.export","label":"Export Reports to PDF/CSV","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:export","blueprint_or_fn":"sales.export"},
    "notes":"Download for sharing/accountant. Manager-and-above."},
   {"id":"10.10","key":"reports.benchmarks","label":"View Industry Benchmarks","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:benchmarks","blueprint_or_fn":"reserved"},
    "notes":"Compare to industry averages. Partner-only."},
 ]},
 {"id": 11, "key": "legal", "name": "Legal & Compliance", "perms": [
   {"id":"11.1","key":"legal.view_docs","label":"View Legal Documents","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:legal docs","blueprint_or_fn":"legal_routes (mostly SOON)"},"notes":"Contracts, agreements, signed docs. Partner-only."},
   {"id":"11.2","key":"legal.upload_docs","label":"Upload Legal Documents","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:legal upload","blueprint_or_fn":"legal_routes"},"notes":"Add legal docs. Partner-only."},
   {"id":"11.3","key":"legal.edit_meta","label":"Edit Legal Document Metadata","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:legal meta","blueprint_or_fn":"legal_routes"},"notes":"Tags, expiration, parties. Partner-only."},
   {"id":"11.4","key":"legal.view_licenses","label":"View Licenses & Permits","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:licenses","blueprint_or_fn":"legal_routes"},"notes":"TABC, food handler, business licenses + expiry. GM-and-above."},
   {"id":"11.5","key":"legal.edit_licenses","label":"Edit Licenses & Permits","status":"reserved","default_roles":CORP_UP,
    "maps_to":{"route":"verify:licenses edit","blueprint_or_fn":"legal_routes"},"notes":"Update license info, renewals. Partner-and-above."},
   {"id":"11.6","key":"legal.view_insurance","label":"View Insurance Information","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:insurance","blueprint_or_fn":"legal_routes"},"notes":"Liability, workers comp, property. Partner-only."},
   {"id":"11.7","key":"legal.edit_insurance","label":"Edit Insurance Information","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:insurance edit","blueprint_or_fn":"legal_routes"},"notes":"Update policies/providers. Partner-only."},
   {"id":"11.8","key":"legal.view_notices","label":"View Legal Notifications","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:legal notices","blueprint_or_fn":"legal_routes"},"notes":"Garnishments, court orders, notices. Partner-only."},
   {"id":"11.9","key":"legal.manage_notices","label":"Manage Legal Notifications","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:legal notices manage","blueprint_or_fn":"legal_routes"},"notes":"Mark actioned, attach response. Partner-only."},
   {"id":"11.10","key":"legal.compliance_cal","label":"View Compliance Calendar","status":"reserved","default_roles":CORP_UP,
    "maps_to":{"route":"verify:compliance cal","blueprint_or_fn":"legal_routes"},"notes":"Filing deadlines, renewals. Partner-and-above."},
 ]},
 {"id": 12, "key": "financial", "name": "Financial", "perms": [
   {"id":"12.1","key":"fin.view_accounts","label":"View Financial Accounts","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:fin accounts","blueprint_or_fn":"reserved"},"notes":"Bank + processor configs. Partner-only."},
   {"id":"12.2","key":"fin.config_accounts","label":"Configure Financial Accounts","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:fin config","blueprint_or_fn":"reserved"},"notes":"Add/change banks + processors. Partner-only. Highly sensitive."},
   {"id":"12.3","key":"fin.view_deposits","label":"View Daily Deposits","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:deposits","blueprint_or_fn":"reserved"},"notes":"Bank deposit confirmations. GM-and-above."},
   {"id":"12.4","key":"fin.view_payroll","label":"View Payroll Setup","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:payroll","blueprint_or_fn":"reserved"},"notes":"Payroll provider config, schedules, tax. Partner-only."},
   {"id":"12.5","key":"fin.edit_payroll","label":"Edit Payroll Setup","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:payroll edit","blueprint_or_fn":"reserved"},"notes":"Modify payroll connection, tax reg. Partner-only."},
   {"id":"12.6","key":"fin.view_ap","label":"View Accounts Payable","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:ap","blueprint_or_fn":"reserved"},"notes":"Total owed to vendors, schedule. GM-and-above."},
   {"id":"12.7","key":"fin.edit_ap","label":"Edit Accounts Payable","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:ap edit","blueprint_or_fn":"reserved"},"notes":"Adjust AP, mark paid, dispute. Partner-only."},
   {"id":"12.8","key":"fin.approve_expense","label":"Approve Large Expenses","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:expense approve","blueprint_or_fn":"reserved"},"notes":"Sign off purchases above a threshold. Partner-only."},
   {"id":"12.9","key":"fin.view_pnl","label":"View Profit & Loss","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:pnl","blueprint_or_fn":"reserved"},"notes":"P&L statements. Partner-only."},
   {"id":"12.10","key":"fin.config_sales_cat","label":"Configure Sales Categories for Accounting","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:sales-cat","blueprint_or_fn":"reserved"},"notes":"Map sales categories to chart of accounts. Partner-only."},
   {"id":"12.11","key":"fin.view_tips","label":"View Tip Pool / Tip Out Records","status":"reserved","default_roles":GM_UP,
    "maps_to":{"route":"verify:tips","blueprint_or_fn":"reserved"},"notes":"How tips are distributed. GM-and-above."},
   {"id":"12.12","key":"fin.config_tips","label":"Configure Tip Pool / Tip Out Rules","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:tips config","blueprint_or_fn":"reserved"},"notes":"Set tip-sharing rules. Partner-only."},
   {"id":"12.13","key":"fin.view_instant_deposit","label":"View Instant Deposit Status","status":"reserved","default_roles":PARTNER,
    "maps_to":{"route":"verify:instant-deposit","blueprint_or_fn":"reserved"},"notes":"Instant deposit availability. Partner-only."},
 ]},
 {"id": 13, "key": "user_perms", "name": "User Permissions (Meta)", "perms": [
   {"id":"13.1","key":"perms.view","label":"View User Permissions","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"/partner/developer/permissions","blueprint_or_fn":"THIS page (we are building it)"},"notes":"See each user's permissions. Partner-only."},
   {"id":"13.2","key":"perms.assign","label":"Assign Permissions to Users","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"/partner/developer/permissions save","blueprint_or_fn":"THIS page (audit-logged)"},"notes":"Add/remove perms on a user. Partner-only. Audit-logged."},
   {"id":"13.3","key":"perms.create_role","label":"Create Role Templates","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"/partner/developer/permissions roles","blueprint_or_fn":"THIS page"},"notes":"Build new role bundles. Partner-only."},
   {"id":"13.4","key":"perms.edit_role","label":"Edit Role Templates","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"/partner/developer/permissions roles","blueprint_or_fn":"THIS page (affects everyone with that role)"},"notes":"Modify what a role includes. Partner-only."},
   {"id":"13.5","key":"perms.delete_role","label":"Delete Role Templates","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"/partner/developer/permissions roles","blueprint_or_fn":"THIS page"},"notes":"Remove a role bundle. Partner-only."},
   {"id":"13.6","key":"perms.assign_role","label":"Assign Roles to Users","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"/partner/developer/permissions save","blueprint_or_fn":"THIS page"},"notes":"Apply a role bundle to a user. Partner-only."},
   {"id":"13.7","key":"perms.override","label":"Override Role Permissions Per User","status":"live","default_roles":PARTNER,
    "maps_to":{"route":"/partner/developer/permissions save","blueprint_or_fn":"THIS page (per-user override on top of role)"},"notes":"Add/remove specific perms on top of a user's role. Partner-only."},
 ]},
 {"id": 14, "key": "driver", "name": "Driver-Specific", "perms": [
   {"id":"14.1","key":"driver.view_own_queue","label":"View Own Driver Queue","status":"live","default_roles":["corporate_driver","partner"],
    "maps_to":{"route":"verify:driver queue","blueprint_or_fn":"ezcater_live / driver app"},"notes":"Own assigned deliveries. Default for drivers."},
   {"id":"14.2","key":"driver.view_all_queue","label":"View All Driver Queues","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:all queues","blueprint_or_fn":"ezcater_routes"},"notes":"Everyone's deliveries. Manager-and-above."},
   {"id":"14.3","key":"driver.update_own","label":"Update Own Delivery Status","status":"live","default_roles":["corporate_driver","partner"],
    "maps_to":{"route":"verify:delivery status","blueprint_or_fn":"orders.mark_picked_up/mark_delivered"},"notes":"Mark picked up / in route / delivered. Drivers."},
   {"id":"14.4","key":"driver.update_others","label":"Update Other Drivers' Status","status":"live","default_roles":MGR_UP,
    "maps_to":{"route":"verify:delivery status override","blueprint_or_fn":"ezcater_routes"},"notes":"Override another driver's status. Manager-and-above."},
   {"id":"14.5","key":"driver.view_earnings","label":"View Driver Earnings","status":"live","default_roles":["corporate_driver","partner","gm","corporate"],
    "maps_to":{"route":"verify:driver earnings","blueprint_or_fn":"ezcater_payroll.py (orders.view_payout)"},"notes":"Tips, base pay, mileage (own for drivers, all for mgrs)."},
   {"id":"14.6","key":"driver.submit_mileage","label":"Submit Mileage / Expense Reports","status":"live","default_roles":["corporate_driver","partner"],
    "maps_to":{"route":"verify:mileage submit","blueprint_or_fn":"ezcater_miles.py"},"notes":"Log miles + expenses for reimbursement. Drivers."},
   {"id":"14.7","key":"driver.approve_mileage","label":"Approve Mileage / Expense Reports","status":"live","default_roles":GM_UP,
    "maps_to":{"route":"verify:mileage approve","blueprint_or_fn":"ezcater_payroll.py"},"notes":"Sign off driver expense submissions. GM-and-above."},
 ]},
]

# ---- Authoritative 'sensitive' flag (money / PII / legal / permission-control).
# Stamped onto every perm so the frontend (ck) renders the lock-icon from this,
# not a client-side heuristic. ----
SENSITIVE_KEYS = {
    # wages + tax IDs + direct deposit
    "emp.view_wages", "emp.edit_wages",
    "emp.view_tax_masked", "emp.view_tax_full", "emp.edit_tax",
    "emp.view_dd", "emp.edit_dd",
    # all financial
    "fin.view_accounts", "fin.config_accounts", "fin.view_deposits",
    "fin.view_payroll", "fin.edit_payroll", "fin.view_ap", "fin.edit_ap",
    "fin.approve_expense", "fin.view_pnl", "fin.config_sales_cat",
    "fin.view_tips", "fin.config_tips", "fin.view_instant_deposit",
    # legal: insurance + official notices (garnishments/court orders)
    "legal.view_insurance", "legal.edit_insurance",
    "legal.view_notices", "legal.manage_notices",
    # vendor payout
    "vendors.pay",
    # permission-control meta (the most sensitive - changing who can do what)
    "perms.view", "perms.assign", "perms.create_role", "perms.edit_role",
    "perms.delete_role", "perms.assign_role", "perms.override",
}
for _cat in CATALOG:
    for _p in _cat["perms"]:
        _p["sensitive"] = _p["key"] in SENSITIVE_KEYS


# ---- Convenience accessors (frontend serializes CATALOG/ROLES/STORES to JSON) ----
def all_permission_keys():
    return [p["key"] for cat in CATALOG for p in cat["perms"]]

def permission_by_key(key):
    for cat in CATALOG:
        for p in cat["perms"]:
            if p["key"] == key:
                return p, cat
    return None, None

def default_role_map():
    """role_key -> set(permission_key) it inherits=ON by default. The migration
    target for the static ROLE_PERMISSIONS dict (partner stays wildcard)."""
    m = {r["key"]: set() for r in ROLES}
    for cat in CATALOG:
        for p in cat["perms"]:
            for rk in p["default_roles"]:
                if rk in m:
                    m[rk].add(p["key"])
    return m
