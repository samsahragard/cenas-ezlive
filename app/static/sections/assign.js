/* ==========================================================================
 * assign.js - Cenas Kitchen Sections / Assign tab (SA-4 lane).
 * Contract: docs/floor_contract.md sections 5, 7, 8, 9, 10.
 * Consumes the SA-3 engine (window.FloorApp from canvas.js) READ-ONLY.
 *
 * Data layer (contract section 9): with ?mock=1 every read resolves from
 * /static/sections/mock_fixture.json by key; writes update the in-memory
 * fixture + console.log + toast. Otherwise real /floor/api/* with loc taken
 * from the shell. Vanilla JS, no build step.
 * ========================================================================== */
(function () {
  "use strict";

  var root = document.getElementById("floorApp");
  if (!root) { return; }
  if (!window.FloorApp || !window.FloorApp.Shell) {
    console.error("[assign] FloorApp engine (canvas.js) not loaded");
    return;
  }

  var MOCK = new URLSearchParams(window.location.search).get("mock") === "1";

  var locations;
  try { locations = JSON.parse(root.getAttribute("data-locations") || "[]"); }
  catch (err) { locations = []; }
  var locDefault = root.getAttribute("data-loc-default") || "uno";
  var isManager = root.getAttribute("data-is-manager") === "1";
  var attentionMinutes = parseInt(root.getAttribute("data-attention-minutes") || "90", 10) || 90;

  var PALETTE_HEX = (window.FloorApp.PALETTE || []).map(function (p) { return p.hex; });

  /* ------------------------------------------------------------ helpers */

  function todayStr() {
    var d = new Date();
    var m = String(d.getMonth() + 1);
    var day = String(d.getDate());
    return d.getFullYear() + "-" + (m.length === 1 ? "0" + m : m) + "-" +
      (day.length === 1 ? "0" + day : day);
  }

  /* Seat capacity is a size-derived ESTIMATE (Toast table config carries no
     seat count): seats = clamp(round(w*h/1600), 2, 12). */
  function seatEstimate(t) {
    var w = (t && t.w != null) ? t.w : 80;
    var h = (t && t.h != null) ? t.h : 80;
    return Math.min(12, Math.max(2, Math.round((w * h) / 1600)));
  }

  function toast(msg, kind) {
    var node = document.createElement("div");
    node.className = "sa4-toast" + (kind ? " sa4-toast--" + kind : "");
    node.textContent = msg;
    root.appendChild(node);
    window.setTimeout(function () { node.classList.add("is-out"); }, 2200);
    window.setTimeout(function () {
      if (node.parentNode) { node.parentNode.removeChild(node); }
    }, 2600);
  }

  /* ---------------------------------------------------------- data layer */

  /* Mock fixture keys mirror /floor/api/* endpoints 1:1 (contract section 9). */
  var MOCK_KEY_BY_ENDPOINT = {
    "floor": "floor",
    "employees": "employees",
    "employees-on-shift": "employees_on_shift",
    "sections": "sections",
    "live": "live",
    "reservations": "reservations",
    "waitlist": "waitlist",
    "history": "history"
  };

  var _fixture = null;
  var _fixturePromise = null;
  function loadFixture() {
    if (_fixture) { return Promise.resolve(_fixture); }
    if (!_fixturePromise) {
      _fixturePromise = fetch("/static/sections/mock_fixture.json")
        .then(function (r) { return r.json(); })
        .then(function (j) { _fixture = j; return j; });
    }
    return _fixturePromise;
  }

  function endpointOf(path) {
    return String(path).split("?")[0]
      .replace(/^\/floor\/api\//, "")
      .replace(/^\/+/, "");
  }

  /* api(path, opts) -> Promise of parsed JSON (with ._status attached).
     Accepts both "/sections" and full "/floor/api/sections?loc=..." paths. */
  function api(path, opts) {
    opts = opts || {};
    if (MOCK) { return mockApi(path, opts); }
    var url = String(path);
    if (url.indexOf("/floor/api/") !== 0) {
      url = "/floor/api" + (url.charAt(0) === "/" ? "" : "/") + url;
    }
    if (url.indexOf("loc=") === -1) {
      url += (url.indexOf("?") === -1 ? "?" : "&") + "loc=" + encodeURIComponent(currentLoc());
    }
    var init = { method: opts.method || "GET", credentials: "same-origin", headers: {} };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = typeof opts.body === "string" ? opts.body : JSON.stringify(opts.body);
    }
    return fetch(url, init).then(function (r) {
      return r.json()
        .catch(function () { return { ok: false, error: "bad_json" }; })
        .then(function (j) { j._status = r.status; return j; });
    });
  }

  function mockApi(path, opts) {
    var method = (opts.method || "GET").toUpperCase();
    var ep = endpointOf(path);
    var body = opts.body;
    if (typeof body === "string") {
      try { body = JSON.parse(body); } catch (err) { body = {}; }
    }
    body = body || {};
    return loadFixture().then(function (fix) {
      if (method === "GET") {
        var key = MOCK_KEY_BY_ENDPOINT[ep.split("/")[0]];
        if (key && fix[key]) {
          var data = fix[key];
          data._status = 200;
          return data;
        }
        return { ok: false, error: "mock_missing", _status: 404 };
      }
      var res = mockWrite(fix, ep, method, body);
      console.log("[floor-mock-write]", method, ep, body, res);
      return res;
    });
  }

  /* In-memory mock writes (contract section 9: update + console.log). */
  function nextId(rows, field) {
    var n = 0;
    (rows || []).forEach(function (r) { if (r[field] > n) { n = r[field]; } });
    return n + 1;
  }

  function isoNow() {
    return new Date().toISOString().replace(/\.\d+Z$/, "Z");
  }

  function bumpCovers(fix, serverGuid, partySize, dir) {
    if (!serverGuid || !fix.live || !fix.live.covers) { return; }
    var c = fix.live.covers[serverGuid];
    if (!c) { c = fix.live.covers[serverGuid] = { live: 0, today: 0 }; }
    c.live = Math.max(0, (c.live || 0) + dir * partySize);
    if (dir > 0) { c.today = (c.today || 0) + partySize; }
  }

  function mockWrite(fix, ep, method, body) {
    var m;
    if (ep === "sections" && method === "POST") {
      var existing = (fix.sections && fix.sections.sections) || [];
      if (existing.length && body.confirm !== true) {
        return { ok: false, error: "exists", exists: true, _status: 409 };
      }
      fix.sections = {
        ok: true,
        date: body.date || todayStr(),
        sections: (body.sections || []).map(function (s, i) {
          return {
            id: i + 1,
            server_employee_guid: s.server_employee_guid,
            server_name: s.server_name || "",
            initials: s.initials || "",
            color: s.color || PALETTE_HEX[i % PALETTE_HEX.length],
            table_guids: (s.table_guids || []).slice()
          };
        })
      };
      return { ok: true, saved: fix.sections.sections.length, _status: 200 };
    }
    if (ep === "seat" && method === "POST") {
      var open = (fix.live && fix.live.open) || [];
      var taken = open.some(function (s) { return s.table_guid === body.table_guid; });
      if (taken) { return { ok: false, error: "occupied", _status: 409 }; }
      var sid = nextId(open, "seating_id");
      open.push({
        seating_id: sid, table_guid: body.table_guid,
        party_size: body.party_size || 2, seated_at: isoNow(), minutes: 0,
        server_employee_guid: body.server_employee_guid || null,
        reservation_id: body.reservation_id || null,
        waitlist_id: body.waitlist_id || null
      });
      if (body.reservation_id && fix.reservations) {
        (fix.reservations.reservations || []).forEach(function (r) {
          if (r.id === body.reservation_id) { r.status = "seated"; r.seating_id = sid; }
        });
      }
      if (body.waitlist_id && fix.waitlist) {
        (fix.waitlist.waitlist || []).forEach(function (w) {
          if (w.id === body.waitlist_id) { w.status = "seated"; w.seating_id = sid; }
        });
      }
      bumpCovers(fix, body.server_employee_guid, body.party_size || 2, 1);
      return { ok: true, seating_id: sid, _status: 200 };
    }
    if (ep === "clear" && method === "POST") {
      var rows = (fix.live && fix.live.open) || [];
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].table_guid === body.table_guid) {
          var gone = rows.splice(i, 1)[0];
          bumpCovers(fix, gone.server_employee_guid, gone.party_size || 0, -1);
          return { ok: true, cleared: true, _status: 200 };
        }
      }
      return { ok: false, error: "not_found", _status: 404 };
    }
    if (ep === "reservations" && method === "POST") {
      var book = (fix.reservations && fix.reservations.reservations) || [];
      var rid = nextId(book, "id");
      var row = {
        id: rid, guest_name: body.guest_name || "", phone: body.phone || "",
        party_size: body.party_size || 2, reserved_for: body.reserved_for || "",
        status: "upcoming", notes: body.notes || "", seating_id: null
      };
      book.push(row);
      return { ok: true, reservation: row, _status: 200 };
    }
    m = ep.match(/^reservations\/(\d+)$/);
    if (m && method === "PATCH") {
      var hitR = null;
      ((fix.reservations && fix.reservations.reservations) || []).forEach(function (r) {
        if (r.id === parseInt(m[1], 10)) {
          ["status", "notes", "party_size", "reserved_for", "guest_name", "phone"]
            .forEach(function (f) { if (body[f] !== undefined) { r[f] = body[f]; } });
          hitR = r;
        }
      });
      if (hitR) { return { ok: true, reservation: hitR, _status: 200 }; }
      return { ok: false, error: "not_found", _status: 404 };
    }
    if (ep === "waitlist" && method === "POST") {
      var wl = (fix.waitlist && fix.waitlist.waitlist) || [];
      var wid = nextId(wl, "id");
      var wrow = {
        id: wid, guest_name: body.guest_name || "", phone: body.phone || "",
        party_size: body.party_size || 2,
        quoted_minutes: body.quoted_minutes != null ? body.quoted_minutes : null,
        joined_at: isoNow(), status: "waiting", seating_id: null
      };
      wl.push(wrow);
      return { ok: true, entry: wrow, _status: 200 };
    }
    m = ep.match(/^waitlist\/(\d+)$/);
    if (m && method === "PATCH") {
      var hitW = null;
      ((fix.waitlist && fix.waitlist.waitlist) || []).forEach(function (w) {
        if (w.id === parseInt(m[1], 10)) {
          ["status", "quoted_minutes", "party_size", "guest_name", "phone"]
            .forEach(function (f) { if (body[f] !== undefined) { w[f] = body[f]; } });
          hitW = w;
        }
      });
      if (hitW) { return { ok: true, entry: hitW, _status: 200 }; }
      return { ok: false, error: "not_found", _status: 404 };
    }
    return { ok: false, error: "mock_unsupported", _status: 400 };
  }

  /* -------------------------------------------------------- shell/canvas */

  var shell = window.FloorApp.Shell.mount(document.getElementById("floorShell"), {
    locations: locations,
    locDefault: locDefault,
    activeTab: "assign",
    isManager: isManager,
    attentionMinutes: attentionMinutes,
    onLocationChange: function (slug) {
      if (slug === state.loc) { return; }
      state.loc = slug;
      reload();
    },
    onAreaChange: function (areaGuid) {
      canvas.filterArea(areaGuid || null);
    }
  });

  function currentLoc() {
    try { return (shell && shell.currentLoc()) || locDefault; }
    catch (err) { return locDefault; }
  }

  var canvas = new window.FloorApp.Canvas(shell.canvasHost, { mode: "assign" });

  var state = {
    loc: locDefault,
    tables: [],
    tablesByGuid: {},
    servers: [],   /* [{guid, name, initials, color}] in panel order */
    assign: {},    /* server guid -> [table guids] */
    armed: null,
    dirty: false
  };

  var listEl = document.getElementById("assignServerList");
  var dirtyEl = document.getElementById("assignDirty");
  var saveBtn = document.getElementById("assignSave");
  var addBtn = document.getElementById("assignAddServer");
  var panelEl = document.getElementById("floorPanel");

  /* ------------------------------------------------------------- loading */

  function reload() {
    state.assign = {};
    state.armed = null;
    state.dirty = false;
    Promise.all([
      api("/floor"),
      api("/employees-on-shift?date=" + todayStr()),
      api("/sections?date=" + todayStr())
    ]).then(function (results) {
      var floor = results[0] || {};
      var onShift = results[1] || {};
      var sec = results[2] || {};
      state.tables = floor.tables || [];
      state.tablesByGuid = {};
      state.tables.forEach(function (t) { state.tablesByGuid[t.guid] = t; });
      shell.setAreas(floor.service_areas || []);
      canvas.setFloor({ tables: state.tables, fixtures: floor.fixtures || [] });

      var servers = [];
      var seen = {};
      /* Existing saved sections first (keep their saved colors)... */
      (sec.sections || []).forEach(function (s) {
        var name = s.server_name || "";
        servers.push({
          guid: s.server_employee_guid,
          name: name || s.server_employee_guid,
          initials: s.initials || window.FloorApp.initials(name),
          color: s.color || PALETTE_HEX[servers.length % PALETTE_HEX.length]
        });
        seen[s.server_employee_guid] = true;
        state.assign[s.server_employee_guid] = (s.table_guids || []).slice();
      });
      /* ...then everyone else on shift (preview color = palette[i % 8]). */
      (onShift.servers || []).forEach(function (s) {
        if (seen[s.employee_guid]) { return; }
        seen[s.employee_guid] = true;
        servers.push({
          guid: s.employee_guid,
          name: s.name,
          initials: s.initials || window.FloorApp.initials(s.name),
          color: s.color || PALETTE_HEX[servers.length % PALETTE_HEX.length]
        });
        state.assign[s.employee_guid] = state.assign[s.employee_guid] || [];
      });
      state.servers = servers;
      paint();
      renderPanel();
      updateStrip();
    }).catch(function (err) {
      console.error("[assign] load failed", err);
      toast("Could not load floor data", "error");
    });
  }

  /* ------------------------------------------------------------ painting */

  function paint() {
    var map = {};
    state.servers.forEach(function (sv) {
      (state.assign[sv.guid] || []).forEach(function (g) {
        /* status 'occupied' gives the engine's tint + initials chip; no
           minutes -> no minutes badge (assign is the dead sheet). */
        map[g] = { status: "occupied", color: sv.color, initials: sv.initials };
      });
    });
    state.tables.forEach(function (t) {
      if (!map[t.guid]) { map[t.guid] = { status: "open" }; }
    });
    canvas.setTableStates(map);
  }

  function updateStrip() {
    shell.setServers(state.servers.map(function (sv) {
      return { guid: sv.guid, name: sv.name, initials: sv.initials,
               color: sv.color, live: 0, today: 0 };
    }));
  }

  function renderPanel() {
    listEl.textContent = "";
    if (!state.servers.length) {
      var empty = document.createElement("p");
      empty.className = "sa4-empty";
      empty.textContent = "No servers on shift yet. Use + Add server to start.";
      listEl.appendChild(empty);
    }
    state.servers.forEach(function (sv) {
      var guids = state.assign[sv.guid] || [];
      var seats = 0;
      guids.forEach(function (g) {
        var t = state.tablesByGuid[g];
        if (t) { seats += seatEstimate(t); }
      });
      var row = document.createElement("button");
      row.type = "button";
      row.className = "floor-list-row sa4-server-row" +
        (state.armed === sv.guid ? " is-armed" : "");
      var av = document.createElement("span");
      av.className = "floor-avatar";
      av.style.background = sv.color;
      av.textContent = sv.initials;
      var bodyEl = document.createElement("span");
      bodyEl.className = "sa4-server-body";
      var nm = document.createElement("strong");
      nm.textContent = sv.name;
      var meta = document.createElement("small");
      meta.textContent = guids.length + (guids.length === 1 ? " table" : " tables") +
        " | ~" + seats + " seats";
      bodyEl.appendChild(nm);
      bodyEl.appendChild(meta);
      row.appendChild(av);
      row.appendChild(bodyEl);
      row.addEventListener("click", function () {
        state.armed = state.armed === sv.guid ? null : sv.guid;
        renderPanel();
      });
      listEl.appendChild(row);
    });
    updateDirty();
  }

  function updateDirty() {
    if (dirtyEl) { dirtyEl.hidden = !state.dirty; }
    if (saveBtn) { saveBtn.classList.toggle("sa4-save--dirty", state.dirty); }
  }

  function markDirty() {
    state.dirty = true;
    paint();
    renderPanel();
  }

  /* -------------------------------------------------- assignment editing */

  function removeEverywhere(tableGuid) {
    Object.keys(state.assign).forEach(function (g) {
      var arr = state.assign[g];
      var i = arr.indexOf(tableGuid);
      if (i !== -1) { arr.splice(i, 1); }
    });
  }

  canvas.on("tableTap", function (tableGuid) {
    if (!state.tablesByGuid[tableGuid]) { return; }
    if (!state.armed) {
      toast("Tap a server first, then tap tables");
      return;
    }
    var mine = state.assign[state.armed] = state.assign[state.armed] || [];
    var i = mine.indexOf(tableGuid);
    if (i !== -1) {
      mine.splice(i, 1);          /* toggle off */
    } else {
      removeEverywhere(tableGuid); /* one section per table */
      mine.push(tableGuid);
    }
    markDirty();
  });

  canvas.on("lassoSelect", function (tableGuids) {
    if (!tableGuids || !tableGuids.length) { return; }
    if (!state.armed) {
      toast("Tap a server first, then lasso tables");
      return;
    }
    var mine = state.assign[state.armed] = state.assign[state.armed] || [];
    tableGuids.forEach(function (g) {
      if (!state.tablesByGuid[g]) { return; }
      if (mine.indexOf(g) === -1) {
        removeEverywhere(g);
        mine.push(g);
      }
    });
    markDirty();
  });

  /* --------------------------------------------------------------- sheet */

  var sheetEl = null;
  function closeSheet() {
    if (sheetEl && sheetEl.parentNode) { sheetEl.parentNode.removeChild(sheetEl); }
    sheetEl = null;
  }
  function openSheet(build) {
    closeSheet();
    sheetEl = document.createElement("div");
    sheetEl.className = "floor-sheet sa4-sheet";
    var head = document.createElement("div");
    head.className = "sa4-sheet-head";
    var close = document.createElement("button");
    close.type = "button";
    close.className = "floor-btn floor-btn--ghost sa4-sheet-close";
    close.textContent = "Close";
    close.addEventListener("click", closeSheet);
    head.appendChild(close);
    sheetEl.appendChild(head);
    build(sheetEl);
    panelEl.appendChild(sheetEl);
    if (sheetEl.scrollIntoView) { sheetEl.scrollIntoView({ block: "nearest" }); }
  }

  /* --------------------------------------------------------- add server */

  function candidates() {
    var present = {};
    state.servers.forEach(function (s) { present[s.guid] = true; });
    return api("/employees-on-shift?date=" + todayStr()).then(function (onShift) {
      var out = [];
      var seen = {};
      ((onShift && onShift.servers) || []).forEach(function (s) {
        if (present[s.employee_guid] || seen[s.employee_guid]) { return; }
        seen[s.employee_guid] = true;
        out.push({ guid: s.employee_guid, name: s.name,
                   initials: s.initials || window.FloorApp.initials(s.name) });
      });
      if (out.length) { return out; }
      /* Everyone on shift is already on the sheet: fall back to the full
         Toast employee list (contract endpoint 6). */
      return api("/employees").then(function (all) {
        ((all && all.employees) || []).forEach(function (e) {
          if (present[e.employee_guid] || seen[e.employee_guid]) { return; }
          seen[e.employee_guid] = true;
          out.push({ guid: e.employee_guid, name: e.name,
                     initials: e.initials || window.FloorApp.initials(e.name) });
        });
        return out;
      });
    });
  }

  /* First palette hex unused in the current panel, wrapping by index. */
  function nextColor() {
    var used = {};
    state.servers.forEach(function (s) { used[(s.color || "").toUpperCase()] = true; });
    for (var i = 0; i < PALETTE_HEX.length; i++) {
      if (!used[PALETTE_HEX[i].toUpperCase()]) { return PALETTE_HEX[i]; }
    }
    return PALETTE_HEX[state.servers.length % PALETTE_HEX.length];
  }

  addBtn.addEventListener("click", function () {
    candidates().then(function (cands) {
      openSheet(function (sheet) {
        var h = document.createElement("h3");
        h.className = "sa4-sheet-title";
        h.textContent = "Add server";
        sheet.appendChild(h);
        if (!cands.length) {
          var p = document.createElement("p");
          p.className = "sa4-empty";
          p.textContent = "Everyone is already on the sheet.";
          sheet.appendChild(p);
          return;
        }
        cands.forEach(function (c) {
          var row = document.createElement("button");
          row.type = "button";
          row.className = "floor-list-row sa4-server-row";
          var av = document.createElement("span");
          av.className = "floor-avatar";
          av.textContent = c.initials;
          var nm = document.createElement("span");
          nm.className = "sa4-server-body";
          var strong = document.createElement("strong");
          strong.textContent = c.name;
          nm.appendChild(strong);
          row.appendChild(av);
          row.appendChild(nm);
          row.addEventListener("click", function () {
            state.servers.push({ guid: c.guid, name: c.name,
                                 initials: c.initials, color: nextColor() });
            state.assign[c.guid] = state.assign[c.guid] || [];
            state.armed = c.guid;
            closeSheet();
            renderPanel();
            updateStrip();
            toast(c.name + " added - tap tables to assign");
          });
          sheet.appendChild(row);
        });
      });
    });
  });

  /* ----------------------------------------------------------- save flow */

  function sectionsPayload() {
    return state.servers
      .filter(function (sv) { return (state.assign[sv.guid] || []).length > 0; })
      .map(function (sv) {
        return {
          server_employee_guid: sv.guid,
          color: sv.color,
          table_guids: (state.assign[sv.guid] || []).slice()
        };
      });
  }

  function save(confirmed) {
    var sections = sectionsPayload();
    if (!sections.length) {
      toast("Assign at least one table before saving");
      return;
    }
    saveBtn.disabled = true;
    api("/sections", {
      method: "POST",
      body: { date: todayStr(), confirm: confirmed === true, sections: sections }
    }).then(function (res) {
      saveBtn.disabled = false;
      if (res && res.ok) {
        state.dirty = false;
        updateDirty();
        closeSheet();
        toast("Sections saved");
        return;
      }
      if (res && (res.error === "exists" || res.exists === true || res._status === 409)) {
        openConfirmOverwrite();
        return;
      }
      toast("Save failed" + (res && res.error ? ": " + res.error : ""), "error");
    }).catch(function () {
      saveBtn.disabled = false;
      toast("Save failed: network error", "error");
    });
  }

  function openConfirmOverwrite() {
    openSheet(function (sheet) {
      var h = document.createElement("h3");
      h.className = "sa4-sheet-title";
      h.textContent = "Overwrite tonight's sections?";
      var p = document.createElement("p");
      p.className = "sa4-hint";
      p.textContent = "Sections already exist for tonight. Saving replaces the whole sheet.";
      var actions = document.createElement("div");
      actions.className = "sa4-sheet-actions";
      var cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "floor-btn floor-btn--ghost";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeSheet);
      var ok = document.createElement("button");
      ok.type = "button";
      ok.className = "floor-btn floor-btn--danger";
      ok.textContent = "Overwrite";
      ok.addEventListener("click", function () { save(true); });
      actions.appendChild(cancel);
      actions.appendChild(ok);
      sheet.appendChild(h);
      sheet.appendChild(p);
      sheet.appendChild(actions);
    });
  }

  saveBtn.addEventListener("click", function () { save(false); });

  reload();
})();
