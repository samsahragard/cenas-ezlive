/* ==========================================================================
 * host.js - Cenas Kitchen Sections / Host tab live map (SA-4 lane).
 * Contract: docs/floor_contract.md sections 2, 5, 7, 8, 9, 10.
 * Consumes the SA-3 engine (window.FloorApp from canvas.js) READ-ONLY and
 * mounts SA-5's FloorReserve panel if present.
 *
 * Live loop: poll GET /floor/api/live every 15s, plus an immediate poll
 * after every action and on visibility-change. States: open = white,
 * occupied = section/server color + initials + minutes since seated,
 * minutes >= attention_minutes -> attention amber (threshold comes from the
 * /floor/api/live payload, never hardcoded).
 *
 * Data layer (contract section 9): with ?mock=1 every read resolves from
 * /static/sections/mock_fixture.json by key; writes update the in-memory
 * fixture + console.log + toast. Otherwise real /floor/api/*.
 * ========================================================================== */
(function () {
  "use strict";

  var LIVE_POLL_MS = 15000; /* contract SA-4 lane: poll /floor/api/live every 15s */
  var PARTY_MIN = 1;
  var PARTY_MAX = 20;
  var PARTY_DEFAULT = 2;

  var root = document.getElementById("floorApp");
  if (!root) { return; }
  if (!window.FloorApp || !window.FloorApp.Shell) {
    console.error("[host] FloorApp engine (canvas.js) not loaded");
    return;
  }

  var MOCK = new URLSearchParams(window.location.search).get("mock") === "1";

  var locations;
  try { locations = JSON.parse(root.getAttribute("data-locations") || "[]"); }
  catch (err) { locations = []; }
  var locDefault = root.getAttribute("data-loc-default") || "uno";
  var isManager = root.getAttribute("data-is-manager") === "1";
  var attentionDefault = parseInt(root.getAttribute("data-attention-minutes") || "90", 10) || 90;

  var PALETTE_HEX = (window.FloorApp.PALETTE || []).map(function (p) { return p.hex; });

  /* ------------------------------------------------------------ helpers */

  function todayStr() {
    var d = new Date();
    var m = String(d.getMonth() + 1);
    var day = String(d.getDate());
    return d.getFullYear() + "-" + (m.length === 1 ? "0" + m : m) + "-" +
      (day.length === 1 ? "0" + day : day);
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
     Accepts both "/live" and full "/floor/api/live?loc=..." paths, so it can
     be handed to FloorReserve as ctx.api unchanged. */
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
        party_size: body.party_size || PARTY_DEFAULT, seated_at: isoNow(), minutes: 0,
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
      bumpCovers(fix, body.server_employee_guid, body.party_size || PARTY_DEFAULT, 1);
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
        party_size: body.party_size || PARTY_DEFAULT, reserved_for: body.reserved_for || "",
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
        party_size: body.party_size || PARTY_DEFAULT,
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
    activeTab: "host",
    isManager: isManager,
    attentionMinutes: attentionDefault,
    onLocationChange: function (slug) {
      if (slug === state.loc) { return; }
      state.loc = slug;
      reloadStatic();
      /* Gate-2 integration fix (ck): the reservations panel reads loc once
         per load - remount it (mount is re-entrant by SA-5 contract) so the
         book/waitlist/history switch stores with the map. */
      if (typeof mountReservePanel === "function") { mountReservePanel(); }
    },
    onAreaChange: function (areaGuid) {
      canvas.filterArea(areaGuid || null);
    }
  });

  function currentLoc() {
    try { return (shell && shell.currentLoc()) || locDefault; }
    catch (err) { return locDefault; }
  }

  var canvas = new window.FloorApp.Canvas(shell.canvasHost, { mode: "view" });

  var state = {
    loc: locDefault,
    tables: [],
    tablesByGuid: {},
    roster: [],              /* [{guid,name,initials,color}] */
    rosterByGuid: {},
    sectionServerByTable: {},/* table guid -> server guid (today's sections) */
    openByTable: {},         /* table guid -> live open seating row */
    live: null
  };

  var panelEl = document.getElementById("floorPanel");
  var liveDot = document.getElementById("hostLiveDot");

  /* ------------------------------------------------------------- loading */

  function reloadStatic() {
    Promise.all([
      api("/floor"),
      api("/sections?date=" + todayStr()),
      api("/employees-on-shift?date=" + todayStr())
    ]).then(function (results) {
      var floor = results[0] || {};
      var sec = results[1] || {};
      var onShift = results[2] || {};
      state.tables = floor.tables || [];
      state.tablesByGuid = {};
      state.tables.forEach(function (t) { state.tablesByGuid[t.guid] = t; });
      shell.setAreas(floor.service_areas || []);
      canvas.setFloor({ tables: state.tables, fixtures: floor.fixtures || [] });

      state.roster = [];
      state.rosterByGuid = {};
      state.sectionServerByTable = {};
      var seen = {};
      (sec.sections || []).forEach(function (s) {
        var name = s.server_name || "";
        var sv = {
          guid: s.server_employee_guid,
          name: name || s.server_employee_guid,
          initials: s.initials || window.FloorApp.initials(name),
          color: s.color || PALETTE_HEX[state.roster.length % PALETTE_HEX.length]
        };
        state.roster.push(sv);
        state.rosterByGuid[sv.guid] = sv;
        seen[sv.guid] = true;
        (s.table_guids || []).forEach(function (g) {
          state.sectionServerByTable[g] = sv.guid;
        });
      });
      (onShift.servers || []).forEach(function (s) {
        if (seen[s.employee_guid]) { return; }
        seen[s.employee_guid] = true;
        var sv = {
          guid: s.employee_guid,
          name: s.name,
          initials: s.initials || window.FloorApp.initials(s.name),
          color: s.color || PALETTE_HEX[state.roster.length % PALETTE_HEX.length]
        };
        state.roster.push(sv);
        state.rosterByGuid[sv.guid] = sv;
      });
      pollLive();
    }).catch(function (err) {
      console.error("[host] load failed", err);
      toast("Could not load floor data", "error");
    });
  }

  /* ------------------------------------------------------------ live loop */

  function minutesFor(s) {
    var computed = null;
    if (s.seated_at) {
      var t = Date.parse(s.seated_at);
      if (!isNaN(t)) { computed = Math.max(0, Math.floor((Date.now() - t) / 60000)); }
    }
    if (MOCK) { return s.minutes != null ? s.minutes : (computed || 0); }
    return computed != null ? computed : (s.minutes || 0);
  }

  function serverFor(seating) {
    var guid = seating.server_employee_guid ||
      state.sectionServerByTable[seating.table_guid] || null;
    return guid ? (state.rosterByGuid[guid] || null) : null;
  }

  function pollLive() {
    return api("/live").then(function (live) {
      if (!live || live.ok === false) { throw new Error(live && live.error); }
      renderLive(live);
      if (liveDot) { liveDot.classList.add("is-ok"); }
      return live;
    }).catch(function (err) {
      console.error("[host] live poll failed", err);
      if (liveDot) { liveDot.classList.remove("is-ok"); }
    });
  }

  function renderLive(live) {
    state.live = live;
    /* Attention threshold comes from the payload (FLOOR_ATTENTION_MINUTES),
       falling back to the page context; never hardcoded. */
    var att = live.attention_minutes != null ? live.attention_minutes : attentionDefault;
    state.openByTable = {};
    (live.open || []).forEach(function (s) { state.openByTable[s.table_guid] = s; });

    var map = {};
    state.tables.forEach(function (t) {
      var s = state.openByTable[t.guid];
      if (!s) {
        map[t.guid] = { status: "open" };
        return;
      }
      var mins = minutesFor(s);
      var sv = serverFor(s);
      map[t.guid] = {
        status: mins >= att ? "attention" : "occupied",
        color: sv ? sv.color : null, /* null -> engine renders neutral */
        initials: sv ? sv.initials : "",
        minutes: mins,
        partySize: s.party_size
      };
    });
    canvas.setTableStates(map);

    var covers = live.covers || {};
    shell.setServers(state.roster.map(function (sv) {
      var c = covers[sv.guid] || { live: 0, today: 0 };
      return { guid: sv.guid, name: sv.name, initials: sv.initials,
               color: sv.color, live: c.live || 0, today: c.today || 0 };
    }));
  }

  /* --------------------------------------------------------------- sheets */

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

  function buildStepper(initial) {
    var value = initial;
    var wrap = document.createElement("div");
    wrap.className = "floor-stepper sa4-stepper";
    var minus = document.createElement("button");
    minus.type = "button";
    minus.textContent = "-";
    var val = document.createElement("span");
    val.className = "floor-stepper-val";
    val.textContent = String(value);
    var plus = document.createElement("button");
    plus.type = "button";
    plus.textContent = "+";
    function set(v) {
      value = Math.min(PARTY_MAX, Math.max(PARTY_MIN, v));
      val.textContent = String(value);
    }
    minus.addEventListener("click", function () { set(value - 1); });
    plus.addEventListener("click", function () { set(value + 1); });
    wrap.appendChild(minus);
    wrap.appendChild(val);
    wrap.appendChild(plus);
    return { el: wrap, value: function () { return value; } };
  }

  /* Server row picker: today's roster, tap to override the prefill. */
  function buildServerPicker(prefillGuid) {
    var selected = prefillGuid || null;
    var wrap = document.createElement("div");
    wrap.className = "sa4-picker";
    var rows = [];
    function paintRows() {
      rows.forEach(function (r) {
        r.el.classList.toggle("is-on", r.guid === selected);
      });
    }
    var options = [{ guid: null, name: "Unassigned", initials: "--", color: null }]
      .concat(state.roster);
    options.forEach(function (sv) {
      var row = document.createElement("button");
      row.type = "button";
      row.className = "floor-list-row sa4-picker-row";
      var av = document.createElement("span");
      av.className = "floor-avatar";
      if (sv.color) { av.style.background = sv.color; }
      av.textContent = sv.initials;
      var nm = document.createElement("span");
      nm.className = "sa4-server-body";
      var strong = document.createElement("strong");
      strong.textContent = sv.name;
      nm.appendChild(strong);
      row.appendChild(av);
      row.appendChild(nm);
      row.addEventListener("click", function () {
        selected = sv.guid;
        paintRows();
      });
      wrap.appendChild(row);
      rows.push({ guid: sv.guid, el: row });
    });
    paintRows();
    return { el: wrap, value: function () { return selected; } };
  }

  function openSeatSheet(table) {
    var prefill = state.sectionServerByTable[table.guid] || null;
    openSheet(function (sheet) {
      var h = document.createElement("h3");
      h.className = "sa4-sheet-title";
      h.textContent = "Seat party - Table " + table.name;
      sheet.appendChild(h);

      var label1 = document.createElement("p");
      label1.className = "sa4-hint";
      label1.textContent = "Party size";
      sheet.appendChild(label1);
      var stepper = buildStepper(PARTY_DEFAULT);
      sheet.appendChild(stepper.el);

      var label2 = document.createElement("p");
      label2.className = "sa4-hint";
      label2.textContent = "Server" + (prefill && state.rosterByGuid[prefill]
        ? " (from tonight's sections)" : "");
      sheet.appendChild(label2);
      var picker = buildServerPicker(prefill);
      sheet.appendChild(picker.el);

      var seat = document.createElement("button");
      seat.type = "button";
      seat.className = "floor-btn sa4-btn-wide"; /* primary blue per contract */
      seat.textContent = "Seat party";
      seat.addEventListener("click", function () {
        seat.disabled = true;
        var payload = {
          loc: currentLoc(),
          table_guid: table.guid,
          party_size: stepper.value()
        };
        if (picker.value()) { payload.server_employee_guid = picker.value(); }
        api("/seat", { method: "POST", body: payload }).then(function (res) {
          if (res && res.ok) {
            toast("Seated party of " + stepper.value() + " at table " + table.name);
            closeSheet();
          } else if (res && res.error === "occupied") {
            toast("Table " + table.name + " is already seated", "error");
            closeSheet();
          } else {
            seat.disabled = false;
            toast("Seat failed" + (res && res.error ? ": " + res.error : ""), "error");
          }
          pollLive(); /* immediate poll after every action */
        }).catch(function () {
          seat.disabled = false;
          toast("Seat failed: network error", "error");
        });
      });
      sheet.appendChild(seat);
    });
  }

  function openOccupiedSheet(table, seating) {
    openSheet(function (sheet) {
      var h = document.createElement("h3");
      h.className = "sa4-sheet-title";
      h.textContent = "Table " + table.name;
      sheet.appendChild(h);

      var sv = serverFor(seating);
      var info = document.createElement("div");
      info.className = "sa4-occupied-info";
      [
        "Party of " + seating.party_size,
        "Server: " + (sv ? sv.name : "Unassigned"),
        minutesFor(seating) + " min seated"
      ].forEach(function (line) {
        var p = document.createElement("p");
        p.textContent = line;
        info.appendChild(p);
      });
      sheet.appendChild(info);

      var clear = document.createElement("button");
      clear.type = "button";
      clear.className = "floor-btn floor-btn--danger sa4-btn-wide";
      clear.textContent = "Clear table";
      clear.addEventListener("click", function () {
        clear.disabled = true;
        api("/clear", {
          method: "POST",
          body: { loc: currentLoc(), table_guid: table.guid }
        }).then(function (res) {
          if (res && res.ok) {
            toast("Table " + table.name + " cleared");
          } else {
            toast("No open seating found on table " + table.name, "error");
          }
          closeSheet();
          pollLive(); /* immediate poll after every action */
        }).catch(function () {
          clear.disabled = false;
          toast("Clear failed: network error", "error");
        });
      });
      sheet.appendChild(clear);
    });
  }

  canvas.on("tableTap", function (guid) {
    /* Gate-2 integration fix (ck): while the reservations panel is in
       pick-a-table mode it owns this tap (it seats the linked party);
       opening the host seat sheet too would double-handle the seating.
       reserve.js flags pick mode with the floor-reserve--picking class. */
    if (document.querySelector(".floor-reserve--picking")) { return; }
    var table = state.tablesByGuid[guid];
    if (!table) { return; }
    var seating = state.openByTable[guid];
    if (seating) { openOccupiedSheet(table, seating); }
    else { openSeatSheet(table); }
  });

  /* ------------------------------------------------- reservations panel */

  /* ctx.api per contract section 10 SA-5: same mock/real routing as this
     tab's own data layer; any reserve-side write also refreshes the map. */
  function reserveApi(path, opts) {
    return api(path, opts).then(function (res) {
      var method = (opts && opts.method ? String(opts.method) : "GET").toUpperCase();
      if (method !== "GET") { pollLive(); }
      return res;
    });
  }

  /* Gate-2 integration fix (ck): the partial loads reserve.js with `defer`,
     so window.FloorReserve does not exist yet when this classic script
     evaluates. Mount now if available, else once the deferred scripts have
     run (DOMContentLoaded). */
  function mountReservePanel() {
    if (!(window.FloorReserve && typeof window.FloorReserve.mount === "function")) {
      return false;
    }
    window.FloorReserve.mount(
      document.getElementById("floorReservePanel") || panelEl,
      {
        loc: function () { return currentLoc(); },
        canvas: canvas,
        shell: shell,
        api: reserveApi
      }
    );
    return true;
  }

  if (!mountReservePanel()) {
    document.addEventListener("DOMContentLoaded", mountReservePanel);
  }

  /* ----------------------------------------------------------- lifecycle */

  window.setInterval(pollLive, LIVE_POLL_MS);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") { pollLive(); }
  });

  reloadStatic();
})();
