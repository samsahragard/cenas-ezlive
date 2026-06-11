# FLOOR CONTRACT - Sections / Floor Map / Host Seating / Reservations
**Status: FROZEN at Gate 0 (2026-06-11, ck orchestrator). No schema or route change without orchestrator sign-off. Deviations go in the log at the bottom.**

This is the single source of truth for the Sections feature. The executable halves of
this contract are:

- `app/floor_models.py` - the frozen schema (written at Gate 0 by the orchestrator; SA-2 owns it from Gate 1 on)
- `app/static/sections/mock_fixture.json` - the mock data SA-3/4/5 build against until integration

---

## 1. Locations

Everything is per-location. Two real stores:

| store slug | Toast location key | label       | Toast restaurant GUID env var       |
|------------|--------------------|-------------|-------------------------------------|
| `uno`      | `copperfield`      | Copperfield | `TOAST_RESTAURANT_GUID_COPPERFIELD` |
| `dos`      | `tomball`          | Tomball     | `TOAST_RESTAURANT_GUID_TOMBALL`     |

- All DB rows are keyed by `location_guid` = the Toast restaurant GUID. Slugs and keys are resolution/display only.
- JSON APIs take `loc=<slug>` (uno|dos). `floor_routes.py` provides `resolve_loc(slug) -> {slug, key, guid, label}`; unknown slug -> 400.
- The page is reachable at `/floor/uno/sections`, `/floor/dos/sections`, `/floor/partner/sections`, `/floor/corporate/sections`. For partner/corporate the default data location is `uno`; the in-page location switcher exposes every store the user can reach (same `accessible_store_slugs` logic as team_workspace).
- Joins are on **table GUID + employee GUID** everywhere. Names are display only.

## 2. Time rules

- All `DateTime` columns store **naive UTC**.
- "Business date" (shift_date, today-filters, covers "today") = local date in `APP_TZ` (America/Chicago), via the same approach as `app.models._local_today`.
- Attention threshold: `FLOOR_ATTENTION_MINUTES` env, default **90** (minutes since seated_at). Served to the client in `/floor/api/live` and the page context; never hardcoded client-side.
- Gate 4 no-show grace: `FLOOR_NOSHOW_GRACE_MINUTES` env, default **20**.

## 3. Schema (9 contract tables + 1 sync-state table)

Defined in `app/floor_models.py` on the shared `app.models.Base`. Prod applies schema by
boot-time `create_all` (alembic is NOT wired on Render) -> `ensure_floor_tables(engine)`
in floor_models is called at floor_routes import time, and SA-2 also adds one alembic
version file for history parity. No process-local state anywhere: every piece of
layout/assignment/seating/reservation state is a DB row (gunicorn multi-worker).

- `toast_tables` (guid PK, location_guid, name, service_area_guid, revenue_center_guid, deleted, last_synced)
- `toast_service_areas` (guid PK, location_guid, name, deleted, last_synced)
- `floor_sync_state` (id, location_guid, resource 'tables'|'service_areas', last_modified, last_run_at)  [SA-1 incremental high-water mark]
- `floor_layouts` (PK location_guid+table_guid, x, y, w, h, shape square|rect|circle|diamond, rotation int degrees)
- `floor_fixtures` (id, location_guid, type wall|label, x, y, w, h, rotation, label)
- `sections` (id, location_guid, shift_date DATE, server_employee_guid, color hex, created_by, created_at; UNIQUE loc+date+server)
- `section_tables` (PK section_id+table_guid)
- `seatings` (id, location_guid, table_guid, party_size, seated_at, seated_by, server_employee_guid_at_seat NULL, cleared_at NULL, reservation_id NULL, waitlist_id NULL)
- `reservations` (id, location_guid, guest_name, phone, party_size, reserved_for DATETIME, status, notes, created_by, created_at, seating_id NULL)
- `waitlist` (id, location_guid, guest_name, phone, party_size, quoted_minutes, joined_at, status, seating_id NULL)

Status enums (frozen):
- reservation: `upcoming | confirmed | arrived | seated | no_show | cancelled`
- waitlist: `waiting | notified | seated | left`  ("notified" is a MANUAL toggle - SMS is out of scope this run; leave the hook clean)

Soft delete only on toast_tables / toast_service_areas (rows Toast stops returning get
`deleted=1`, never DELETE - historical seatings join on old GUIDs).

## 4. Coordinate system (canvas + layout rows share it)

- Abstract floor space: **1000 x 620 units**, SVG viewBox `0 0 1000 620`, responsive width.
- Snap grid: 10 units; faint grid lines every 20 units.
- `x,y` = top-left of the shape's bounding box; `w,h` = bounding box size. Circle: w = diameter (h=w). Diamond = square with `rotation=45`. Default new table: 80x80 square at rotation 0.
- Fixtures use the same space. `wall` renders as a light `#E8E8E8` strip; `label` renders as an outlined box with its `label` text centered (BAR, HOST, KITCHEN...).

## 5. Server identity / palette (frozen, BE and FE identical)

8 colors, in this exact order (index 0-7):

| idx | key    | hex       |
|-----|--------|-----------|
| 0   | teal   | `#14B8A6` |
| 1   | purple | `#8B5CF6` |
| 2   | blue   | `#3B82F6` |
| 3   | pink   | `#EC4899` |
| 4   | green  | `#22C55E` |
| 5   | amber  | `#F59E0B` |
| 6   | red    | `#EF4444` |
| 7   | slate  | `#64748B` |

- Stored on `sections.color` as hex. POST sections without explicit color -> server assigns first palette hex unused in that (loc, date), wrapping by index when more than 8 servers.
- Pre-assignment preview color for a server list = `palette[i % 8]` in list order.
- Initials: first letter of first + first letter of last word of the display name, uppercased ("Kayla Gomez" -> "KG").
- One visual system: section color == table tint == avatar color.

## 6. JSON API (blueprint `floor`, url_prefix `/floor`, all JSON)

Response envelope: success `{"ok": true, ...}`; failure `{"ok": false, "error": "<code>"}` with a proper HTTP status. Auth failures: 403. Unknown loc: 400.

Auth (uses existing helpers `require_dashboard_access` / `has_dashboard_access` / `current_role_is` from `app.web.dashboard_access`):
- ALL `/floor/api/*`: require `dash.operations` access.
- MANAGER-ONLY (= `dash.operations` AND NOT role `expo` - the `_operations_full_access_ok` pattern): `PUT layout`, `PUT fixtures`, `POST sections`, `POST sync`. Everything else (seat/clear/reservations/waitlist) is host-stand level: any dash.operations holder.

| # | Method+Path | Purpose |
|---|-------------|---------|
| 1 | `GET /floor/api/floor?loc=` | tables (non-deleted, layout merged) + unplaced list + fixtures + service_areas |
| 2 | `PUT /floor/api/layout?loc=` | full replace of floor_layouts for loc. Body `{"tables":[{table_guid,x,y,w,h,shape,rotation}]}` (manager) |
| 3 | `PUT /floor/api/fixtures?loc=` | full replace of floor_fixtures for loc. Body `{"fixtures":[{type,x,y,w,h,rotation,label}]}` (manager) |
| 4 | `GET /floor/api/sections?loc=&date=YYYY-MM-DD` | sections for the shift (default: today) |
| 5 | `POST /floor/api/sections?loc=` | replace the shift's full assignment set (manager). Body `{"date","confirm",sections:[{server_employee_guid,color?,table_guids[]}]}`. If sections already exist for (loc,date) and confirm!=true -> **409** `{"ok":false,"error":"exists","exists":true}` |
| 6 | `GET /floor/api/employees?loc=` | all Toast employees `[{employee_guid,name,initials}]` (from toast_client.fetch_employees, cached) |
| 7 | `GET /floor/api/employees-on-shift?loc=&date=` | servers on shift `{servers:[{employee_guid,name,initials,color}], source:"shifts"|"employees"}` (fetch_shifts for the date; falls back to full employee list, source tells which) |
| 8 | `GET /floor/api/live?loc=` | `{open:[{seating_id,table_guid,party_size,seated_at,minutes,server_employee_guid,reservation_id,waitlist_id}], covers:{<employee_guid>:{live,today}}, attention_minutes}` |
| 9 | `POST /floor/api/seat` | Body `{loc,table_guid,party_size?,server_employee_guid?,reservation_id?,waitlist_id?}`. Open seating exists on table -> **409** `{"error":"occupied"}`. party_size resolution: explicit > linked reservation/waitlist > 400. server resolution: explicit > today's section containing table > NULL. Links back: reservation/waitlist row gets status='seated' + seating_id; seating stores reservation_id/waitlist_id |
| 10 | `POST /floor/api/clear` | Body `{loc,table_guid}` -> sets cleared_at on the open seating; 404 if none |
| 11 | `GET /floor/api/reservations?loc=&date=` | that business date's book (default today), ordered by reserved_for |
| 12 | `POST /floor/api/reservations` | Body `{loc,guest_name,party_size,reserved_for,phone?,notes?}` (ISO datetime, local-naive accepted = APP_TZ). Gate 4 adds duplicate guard here |
| 13 | `PATCH /floor/api/reservations/<id>` | Body any of `{status,notes,party_size,reserved_for,guest_name,phone}`; status validated against enum |
| 14 | `GET /floor/api/waitlist?loc=&include_done=0|1` | default: today's `waiting|notified` only; include_done adds today's `seated|left` |
| 15 | `POST /floor/api/waitlist` | Body `{loc,guest_name,party_size,phone?,quoted_minutes?}` |
| 16 | `PATCH /floor/api/waitlist/<id>` | Body any of `{status,quoted_minutes,party_size,guest_name,phone}` |
| 17 | `GET /floor/api/history?loc=&date=` | `{seatings:[all that date, incl cleared, with server+table names], reservations:[terminal: seated/no_show/cancelled], waitlist:[terminal: seated/left]}` |
| 18 | `POST /floor/api/sync?loc=` | (manager) runs `toast_config_sync.sync_location(key)`; returns its counts dict |

Serialization: dates `YYYY-MM-DD`; datetimes ISO-8601 UTC with `Z`. Employee/table
objects always carry `*_guid` + display `name`.

## 7. Page routes (on the SAME `floor` blueprint - floor_bp is fully self-contained; orchestrator registers it in app/__init__.py at Gate 2)

`GET /floor/<store_slug>/sections?tab=assign|host|map[&mock=1]` - auth `require_dashboard_access("dash.operations", store_slug)`.
- `tab=assign` (default) -> renders `sections_assign.html`
- `tab=host` -> `sections_host.html`
- `tab=map` -> `sections_map.html`, **manager-only** (else 403)

Template context (frozen): `store_slug`, `active_tab`, `locations_json` (JSON string of `[{slug,key,label}]` the user can reach), `loc_default`, `is_manager` (bool), `attention_minutes` (int), `user_name` (str or "").

Templates are STANDALONE full HTML pages (no dashboard base/sidebar - they are iframed
by the Operations dashboard and must work full-screen on a phone). Root element:

```html
<div class="floor-app" id="floorApp"
     data-store="{{ store_slug }}" data-active-tab="assign"
     data-locations='{{ locations_json }}' data-loc-default="{{ loc_default }}"
     data-is-manager="{{ '1' if is_manager else '0' }}"
     data-attention-minutes="{{ attention_minutes }}">
  <div id="floorShell"></div>
  <div id="floorPanel"><!-- tab-specific markup --></div>
</div>
```

Assets per tab: `sections.css` + `canvas.js` (shared, SA-3) + the tab's own JS/CSS.
`sections_host.html` carries this exact slot comment where the reservations panel goes
(orchestrator swaps it for the include at Gate 2):
`<!-- FLOOR_RESERVE_PANEL_INCLUDE_SLOT: {% include "sections_reserve_panel.html" %} -->`

## 8. Front-end engine contract (canvas.js, owned by SA-3; SA-4/SA-5 consume READ-ONLY)

`canvas.js` defines `window.FloorApp`:

- `FloorApp.PALETTE` - the section-5 array `[{key,hex},...]`.
- `FloorApp.initials(name)` -> "KG".
- `FloorApp.Shell.mount(rootEl, opts)` - builds the shared shell (top bar: location
  switcher chips + service-area chips; tab links Assign/Host/Map with active underline,
  Map hidden when not manager, mock param preserved; server strip; canvas host; panel
  host gets the page's `#floorPanel`). opts: `{locations, locDefault, activeTab,
  isManager, attentionMinutes, onLocationChange(cb), onAreaChange(cb)}`. Returns shell:
  - `shell.canvasHost` (element), `shell.currentLoc()`, `shell.currentArea()` (guid|null)
  - `shell.setServers([{guid,name,initials,color,live,today}])` - strip renders avatar chip + name + cover counts in the EXACT text pattern `` `${live} live | ${today} today` ``
  - `shell.setAreas([{guid,name}])`
  - `shell.setBadge(tabKey, n)` (Gate 4 host badge; n=0 hides)
- `new FloorApp.Canvas(hostEl, {mode})`, mode `view|assign|setup`:
  - `setFloor({tables, fixtures})` - tables = floor rows merged w/ layout (unplaced tables only render in setup tray)
  - `setTableStates(map)` - `{<table_guid>: {status:'open'|'occupied'|'attention', color, initials, minutes, partySize}}`
  - `filterArea(serviceAreaGuid|null)`
  - `setSelected(guids)`; events via `on(name, cb)`: `tableTap(guid)`, `lassoSelect(guids)` (assign mode), `change()` (setup mode edits)
  - setup mode: `addTable(guid, shape)`, `getLayout()`, `getFixtures()`, `startFixtureDraw('wall'|'label')`, `rotateSelected()` (45-degree toggle), `setShapeOfSelected(shape)`, `resize via drag handles`, `removeSelected()`
  - Rendering (frozen visuals): open = white `#FFFFFF` fill + dark `#111317` table name; occupied = server-color fill + white text + circular initials chip; attention = `#F5B81C`; occupied-with-unknown-server = neutral `#6B7280`. Faint grid. Fixtures per section 4.

### Design tokens (all in sections.css, scoped under `.floor-app`)
panel bg `#111317`; card `#1C1F24` radius 14px; canvas `#2F3338`; text white / secondary `#9AA0A8`; primary action `#2F6FED` fully-rounded buttons; status pills fully rounded: upcoming = gray outline, confirmed = purple, arrived = mint, seated = green, no_show/left = slate. Mobile (<768px): canvas on top, panel below as a sheet, tabs as underlined chips; tablet/desktop: panel left, canvas right. Nothing may leak outside `.floor-app`.

## 9. Mock fixture (`app/static/sections/mock_fixture.json`)

Top-level keys mirror endpoints 1:1 - each value is EXACTLY what the endpoint returns:
`floor`, `employees`, `employees_on_shift`, `sections`, `live`, `reservations`,
`waitlist`, `history`. Tab JS: when `?mock=1`, fetch the fixture once and resolve all
API reads from it (writes: update in-memory + console.log). Mapping is by key name.

## 10. SA module contracts

- **SA-1** `app/services/toast_config_sync.py`: `sync_location(location_key:str) -> dict` (counts: `tables_upserted, tables_soft_deleted, service_areas_upserted, service_areas_soft_deleted, source:"full"|"incremental"`), `sync_all() -> dict[key->counts]`. Upsert by guid; lastModified incremental via floor_sync_state; rows missing from a FULL pull get deleted=1 (incremental pulls never soft-delete). Runnable as `python -m app.services.toast_config_sync [key]` for cron. May add `fetch_service_areas` (and helpers strictly needed) to toast_client.py - the ONLY SA allowed to touch that file.
- **SA-2** `app/web/floor_routes.py` (+ owns floor_models.py from Gate 1, + one migrations/versions file): implements sections 6+7 exactly. Reads sync counts by calling SA-1's function (import inside the route, so floor_routes imports clean before SA-1 lands).
- **SA-3** `app/templates/sections_map.html`, `app/static/sections/canvas.js`, `app/static/sections/sections.css`, `app/static/sections/map.js` (+map.css if wanted): the engine (section 8) + Map Setup tab (tray of unplaced tables, drag/place/resize/rotate/shape, fixture tools, Save -> PUT layout + PUT fixtures).
- **SA-4** `app/templates/sections_assign.html`, `app/templates/sections_host.html`, `app/static/sections/assign.js/.css`, `app/static/sections/host.js/.css`: Assign (tap/lasso -> assign to server, instant tint, side panel servers-on-shift w/ table count + seat capacity, Save w/ confirm-on-overwrite 409 flow) + Host (live map w/ minutes + amber, tap open -> bottom sheet party-size stepper + server prefill -> blue "Seat party"; tap occupied -> Clear table; strip updates live - poll `/floor/api/live` every 15s). Leaves the reserve include slot.
- **SA-5** `app/templates/sections_reserve_panel.html` (partial: markup + its own script/link tags for reserve.js/.css), `app/static/sections/reserve.js/.css`: Reservations/Waitlist/History sub-tabs with counts in labels, search, date pager, "+ Add reservation" sheet, status pills, entry tap -> Seat -> table pick on map -> POST seat with reservation_id/waitlist_id. Exposes `window.FloorReserve.mount(panelEl, ctx)` where ctx = `{loc()->slug, canvas, shell, api(path, opts)}`; host.js calls it if present (`if (window.FloorReserve) ...`).

## 11. Gate 3 (build only after Gate 2 green; report, do NOT touch scoring)

`app/floor_performance.py`: join seatings + section_tables against Toast orders for a
business date (checks carry table GUID + server GUID) -> per-server covers, sales per
section, planned-vs-actual server per table. Pure module + CLI (`python -m
app.floor_performance <loc> <date>`); NO changes to existing server-performance code;
output = a report of what WOULD feed scoring. Sam approves separately.

## 12. Gate 4 (after Gate 2; frozen now so no re-freeze)

- No-show auto-flag: applied lazily inside GET reservations/history/live (no cron, no in-memory state): status in (upcoming, confirmed) AND reserved_for + grace < now -> status='no_show' (persisted on read).
- Duplicate-guest guard: POST reservations with same loc + same phone (non-empty) + reserved_for within +/-90min of an existing non-cancelled row -> **409** `{"error":"duplicate","duplicate":true}` unless `confirm:true`.
- Host tab badge: count of today's reservations with status in (upcoming, confirmed, arrived) -> `shell.setBadge('host', n)` wired from live polling payload (add `reservation_badge` int to `/floor/api/live` response).
- History backfill view: history endpoint accepts `days=N` (default 1, max 30) returning per-day buckets.

## 13. Verification protocol (every SA, before reporting done)

1. **Self-test green**: own pytest file green locally (`tests/test_floor_<lane>.py` - sync|routes|canvas|assign_host|reserve); `python -m py_compile` on every touched .py; templates render (Jinja compile).
2. **Running app**: boot the real app from the worktree (`python -m flask --app wsgi run -p <your port>` - SA-2:8801 SA-3:8802 SA-4:8803 SA-5:8804; SA-1 runs its module against real Toast instead, READ-ONLY) and prove your surface works (curl JSON endpoints / load pages with ?mock=1 and check 200 + no template errors; auth-forge via Flask test client session like tests/test_dashboard_access_routes.py when needed). Kill your server when done. After ANY background command: verify process alive + exit code 0 before narrating.
3. **Live on origin/main**: collective, at Gate 2 (orchestrator).

Hard rules: never edit outside your territory (stop and report instead); never commit or push (orchestrator owns git); delete any scratch files; no secrets in code or fixtures; nothing writes to Toast; no process-local state for app data.

## 14. Deviation log

- 2026-06-11 Gate 0: added `deleted` + `last_synced` to toast_service_areas (soft-delete consistency); added `floor_sync_state` table (SA-1 lastModified high-water); added routes beyond the mission list: GET employees, GET history, POST sync (operationally required); "Sections" nav lands as an Operations-dashboard tab adjacent to Performance (that tab strip IS where Performance lives in this app).
- 2026-06-11 Gate 0 (amended pre-unlock): page routes live on floor_bp at `/floor/<store_slug>/sections` (NOT attached to store_bp) so floor_routes.py is fully self-contained and testable without touching app/__init__.py; the orchestrator registers floor_bp in __init__.py once, at Gate 2. For local verification: `from app import create_app; app = create_app(); app.register_blueprint(floor_bp)` then test_client / dev server.
- 2026-06-11 Gate 2 (orchestrator integration): floor_bp registered in app/__init__.py; "Sections" tab added to _OPERATIONS_DASH_TABS in store_routes.py directly after Performance (expo's filtered tab list excludes it, consistent with the other manager surfaces); host include slot swapped live; GET /floor/api/floor now runs a one-time best-effort bootstrap config sync when a location has zero toast_tables rows (fresh prod deploy self-heals; manual refresh stays POST /floor/api/sync).
- Build-phase deviations accepted at Gate 1 review: revenue_center_guid is NULL at both live stores (Toast returns revenueCenter: null - stored when present); Toast config payload has no per-entity lastModified so the high-water mark is pull-start-time minus 5-min overlap; assign-panel seat capacity is a size-derived ESTIMATE seats=clamp(round(w*h/1600),2,12) (Toast exposes no seat counts); assign-mode tint reuses the 'occupied' canvas state (tinted+chip, no minutes badge); client tab JS uses device-local "today" (host-stand devices are store-local); SA-5 added a "+ Add to waitlist" sheet (route 15 was otherwise unreachable in the UI).
