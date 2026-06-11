/* ==========================================================================
 * map.js - Map Setup tab (SA-3). docs/floor_contract.md sections 7-8 + the
 * SA-3 module contract: tray of unplaced tables, drag onto canvas to place
 * (snap 10), select -> resize / rotate-45 / shape picker / remove, fixture
 * tools (wall strips + labeled boxes), Save = PUT /floor/api/layout +
 * PUT /floor/api/fixtures.
 *
 * Data layer: ?mock=1 -> resolve all reads from
 * /static/sections/mock_fixture.json by key ("floor"); writes log + toast.
 * Otherwise fetch the real endpoints with the loc from the shell switcher.
 * ========================================================================== */
(function () {
  "use strict";

  var root = document.getElementById("floorApp");
  if (!root || !window.FloorApp) return;

  var IS_MOCK = new URLSearchParams(window.location.search).get("mock") === "1";
  var MOCK_URL = "/static/sections/mock_fixture.json";

  var locations = [];
  try {
    locations = JSON.parse(root.dataset.locations || "[]");
  } catch (e) {
    locations = [];
  }

  var shell = FloorApp.Shell.mount(root, {
    locations: locations,
    locDefault: root.dataset.locDefault || "uno",
    activeTab: root.dataset.activeTab || "map",
    isManager: root.dataset.isManager === "1",
    attentionMinutes: parseInt(root.dataset.attentionMinutes || "90", 10),
    onLocationChange: function () { loadFloor(); },
    onAreaChange: function (areaGuid) { canvas.filterArea(areaGuid); }
  });

  var canvas = new FloorApp.Canvas(shell.canvasHost, { mode: "setup" });

  var trayEl = document.getElementById("mapTray");
  var statusEl = document.getElementById("mapStatus");
  var saveBtn = document.getElementById("mapSave");
  var toastEl = document.getElementById("mapToast");
  var allTables = []; /* full registry for the current loc: placed + unplaced */
  var dirty = false;
  var mockData = null;

  /* ---------------------------------------------------------- data layer */

  function fetchMock() {
    if (mockData) return Promise.resolve(mockData);
    return fetch(MOCK_URL).then(function (r) { return r.json(); }).then(function (json) {
      mockData = json;
      return mockData;
    });
  }

  function apiGetFloor() {
    if (IS_MOCK) {
      /* reads resolve from the fixture by key name */
      return fetchMock().then(function (data) { return data.floor; });
    }
    return fetch("/floor/api/floor?loc=" + encodeURIComponent(shell.currentLoc()), {
      credentials: "same-origin"
    }).then(function (r) { return r.json(); });
  }

  function apiPut(path, body) {
    if (IS_MOCK) {
      console.log("[mock] PUT " + path, body);
      return Promise.resolve({ ok: true, mock: true });
    }
    return fetch(path + "?loc=" + encodeURIComponent(shell.currentLoc()), {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (json) {
        if (!r.ok || !json.ok) throw new Error(json.error || ("HTTP " + r.status));
        return json;
      });
    });
  }

  /* --------------------------------------------------------------- toast */

  var toastTimer = null;
  function toast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 2200);
  }

  function setStatus(msg) {
    if (statusEl) statusEl.textContent = msg || "";
  }

  /* ----------------------------------------------------------- load+tray */

  function loadFloor() {
    setStatus("Loading floor...");
    apiGetFloor().then(function (floor) {
      floor = floor || {};
      var placed = (floor.tables || []).map(function (t) { return t; });
      var unplaced = (floor.unplaced || []).map(function (t) {
        return { guid: t.guid, name: t.name, service_area_guid: t.service_area_guid || null,
                 x: null, y: null };
      });
      allTables = placed.concat(unplaced);
      canvas.setFloor({ tables: allTables, fixtures: floor.fixtures || [] });
      shell.setAreas(floor.service_areas || []);
      canvas.filterArea(shell.currentArea());
      dirty = false;
      renderTray();
      setStatus(IS_MOCK ? "Mock floor loaded." : "Floor loaded.");
    }).catch(function (err) {
      setStatus("Could not load floor: " + err.message);
    });
  }

  function unplacedTables() {
    var placedGuids = {};
    canvas.getLayout().forEach(function (row) { placedGuids[row.table_guid] = true; });
    return allTables.filter(function (t) { return !placedGuids[t.guid]; });
  }

  function renderTray() {
    if (!trayEl) return;
    trayEl.textContent = "";
    var list = unplacedTables();
    if (!list.length) {
      var empty = document.createElement("div");
      empty.className = "floor-map-tray-empty";
      empty.textContent = "All tables are placed.";
      trayEl.appendChild(empty);
      return;
    }
    list.forEach(function (t) {
      var chip = document.createElement("div");
      chip.className = "floor-tray-chip";
      chip.textContent = t.name || "?";
      chip.dataset.guid = t.guid;
      bindTrayDrag(chip, t);
      trayEl.appendChild(chip);
    });
  }

  /* drag a tray chip onto the canvas to place (works with touch: pointer
     events + touch-action none on the chip) */
  function bindTrayDrag(chip, table) {
    chip.addEventListener("pointerdown", function (ev) {
      ev.preventDefault();
      try { chip.setPointerCapture(ev.pointerId); } catch (e) { /* noop */ }
      var ghost = null;
      var moved = false;

      function onMove(mv) {
        if (!moved && (Math.abs(mv.clientX - ev.clientX) > 4 || Math.abs(mv.clientY - ev.clientY) > 4)) {
          moved = true;
          ghost = document.createElement("div");
          ghost.className = "floor-tray-ghost";
          ghost.textContent = table.name || "?";
          root.appendChild(ghost);
        }
        if (ghost) {
          ghost.style.left = mv.clientX + "px";
          ghost.style.top = mv.clientY + "px";
        }
      }

      function onUp(up) {
        chip.removeEventListener("pointermove", onMove);
        chip.removeEventListener("pointerup", onUp);
        chip.removeEventListener("pointercancel", onUp);
        if (ghost && ghost.parentNode) ghost.parentNode.removeChild(ghost);
        if (up.type === "pointercancel") return;
        var svgRect = canvas.svg.getBoundingClientRect();
        var overCanvas = up.clientX >= svgRect.left && up.clientX <= svgRect.right &&
                         up.clientY >= svgRect.top && up.clientY <= svgRect.bottom;
        if (moved && overCanvas) {
          var p = canvas.clientToCanvas(up.clientX, up.clientY);
          canvas.addTable(table.guid, currentShape, p.x - 40, p.y - 40);
        } else if (!moved) {
          /* simple tap: place at canvas center */
          canvas.addTable(table.guid, currentShape);
        }
      }

      chip.addEventListener("pointermove", onMove);
      chip.addEventListener("pointerup", onUp);
      chip.addEventListener("pointercancel", onUp);
    });
  }

  /* ----------------------------------------------------------- tool wires */

  var currentShape = "square";

  function wire(id, fn) {
    var btn = document.getElementById(id);
    if (btn) btn.addEventListener("click", fn);
    return btn;
  }

  var shapeBtns = ["square", "rect", "circle", "diamond"].map(function (shape) {
    return wire("mapShape-" + shape, function () {
      currentShape = shape;
      ["square", "rect", "circle", "diamond"].forEach(function (s) {
        var b = document.getElementById("mapShape-" + s);
        if (b) b.classList.toggle("active", s === shape);
      });
      canvas.setShapeOfSelected(shape);
    });
  });
  if (shapeBtns[0]) shapeBtns[0].classList.add("active");

  wire("mapRotate", function () { canvas.rotateSelected(); });
  wire("mapRemove", function () { canvas.removeSelected(); });
  wire("mapDrawWall", function () {
    canvas.startFixtureDraw("wall");
    setStatus("Drag on the canvas to draw a wall strip.");
  });
  wire("mapDrawLabel", function () {
    canvas.startFixtureDraw("label");
    setStatus("Drag on the canvas to draw a labeled box.");
  });

  canvas.on("change", function () {
    dirty = true;
    renderTray();
    setStatus("Unsaved changes.");
  });

  /* Save = PUT layout + PUT fixtures */
  if (saveBtn) {
    saveBtn.addEventListener("click", function () {
      saveBtn.disabled = true;
      setStatus("Saving...");
      var layoutBody = { tables: canvas.getLayout() };
      var fixturesBody = { fixtures: canvas.getFixtures() };
      apiPut("/floor/api/layout", layoutBody)
        .then(function () { return apiPut("/floor/api/fixtures", fixturesBody); })
        .then(function () {
          dirty = false;
          setStatus(IS_MOCK ? "Saved (mock - logged to console)." : "Layout saved.");
          toast(IS_MOCK ? "Mock save OK - check console" : "Floor layout saved");
        })
        .catch(function (err) {
          setStatus("Save failed: " + err.message);
          toast("Save failed");
        })
        .then(function () { saveBtn.disabled = false; });
    });
  }

  loadFloor();
})();
