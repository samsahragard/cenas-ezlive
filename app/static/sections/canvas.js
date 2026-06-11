/* ==========================================================================
 * canvas.js - Cenas Kitchen Floor shared front-end engine (SA-3, sole owner)
 * FROZEN CONTRACT: docs/floor_contract.md section 8. SA-4 / SA-5 consume
 * this READ-ONLY. Vanilla JS, zero external libs, no build step.
 *
 * window.FloorApp:
 *   FloorApp.PALETTE                      frozen 8-color array [{key,hex},...]
 *   FloorApp.initials(name)            -> "KG"
 *   FloorApp.Shell.mount(rootEl, opts) -> shell
 *   new FloorApp.Canvas(hostEl, {mode})   mode: 'view' | 'assign' | 'setup'
 * ========================================================================== */
(function () {
  "use strict";

  var SVG_NS = "http://www.w3.org/2000/svg";
  var FLOOR_W = 1000;
  var FLOOR_H = 620;
  var GRID_STEP = 20; /* faint grid lines every 20 units */
  var SNAP = 10;      /* snap grid */
  var TAP_SLOP = 6;   /* css px of movement that still counts as a tap */

  /* Frozen visuals (contract sections 4-5 + 8) */
  var COLOR_OPEN_FILL = "#FFFFFF";
  var COLOR_NAME_DARK = "#111317";
  var COLOR_ATTENTION = "#F5B81C";
  var COLOR_UNKNOWN = "#6B7280";
  var COLOR_WALL = "#E8E8E8";
  var COLOR_SELECT = "#2F6FED";

  /* Frozen 8-color server palette (contract section 5; index order matters) */
  var PALETTE = [
    { key: "teal", hex: "#14B8A6" },
    { key: "purple", hex: "#8B5CF6" },
    { key: "blue", hex: "#3B82F6" },
    { key: "pink", hex: "#EC4899" },
    { key: "green", hex: "#22C55E" },
    { key: "amber", hex: "#F59E0B" },
    { key: "red", hex: "#EF4444" },
    { key: "slate", hex: "#64748B" }
  ];

  /* "Kayla Gomez" -> "KG" (first letter of first + first letter of last word) */
  function initials(name) {
    var words = String(name || "").trim().split(/\s+/).filter(Boolean);
    if (!words.length) return "";
    var first = words[0].charAt(0);
    var last = words[words.length - 1].charAt(0);
    return (words.length === 1 ? first : first + last).toUpperCase();
  }

  function snap(v) {
    return Math.round(v / SNAP) * SNAP;
  }

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  function el(tag, attrs, text) {
    var node = document.createElementNS(SVG_NS, tag);
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) {
        node.setAttribute(k, attrs[k]);
      }
    }
    if (text != null) node.textContent = text;
    return node;
  }

  function htmlEl(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  /* ========================================================== Shell ===== */

  var Shell = {
    /**
     * Builds the shared shell inside the page's #floorShell and arranges the
     * page's #floorPanel. opts:
     *   {locations, locDefault, activeTab, isManager, attentionMinutes,
     *    onLocationChange(cb), onAreaChange(cb)}
     */
    mount: function (rootEl, opts) {
      opts = opts || {};
      var appEl = rootEl.classList && rootEl.classList.contains("floor-app")
        ? rootEl
        : (rootEl.closest && rootEl.closest(".floor-app")) || rootEl;
      var shellEl = appEl.querySelector("#floorShell") || rootEl;
      var panelEl = appEl.querySelector("#floorPanel");
      shellEl.classList.add("floor-shell");
      if (panelEl) panelEl.classList.add("floor-panel");

      var locations = opts.locations || [];
      var currentLoc = opts.locDefault || (locations[0] && locations[0].slug) || "";
      var currentArea = null; /* null = All */
      var locListeners = [];
      var areaListeners = [];
      if (typeof opts.onLocationChange === "function") locListeners.push(opts.onLocationChange);
      if (typeof opts.onAreaChange === "function") areaListeners.push(opts.onAreaChange);

      /* --- top bar: location chips + tab links --- */
      var topbar = htmlEl("div", "floor-topbar");
      var locRow = htmlEl("div", "floor-locrow");
      locRow.style.display = "flex";
      locRow.style.gap = "6px";
      topbar.appendChild(locRow);

      function renderLocChips() {
        locRow.textContent = "";
        locations.forEach(function (loc) {
          var chip = htmlEl("button", "floor-chip", loc.label || loc.slug);
          chip.type = "button";
          chip.dataset.loc = loc.slug;
          if (loc.slug === currentLoc) chip.classList.add("active");
          chip.addEventListener("click", function () {
            if (currentLoc === loc.slug) return;
            currentLoc = loc.slug;
            renderLocChips();
            locListeners.forEach(function (cb) { cb(currentLoc); });
          });
          locRow.appendChild(chip);
        });
      }
      renderLocChips();

      /* tab links: Assign / Host / Map as underlined chips, Map manager-only,
         mock=1 param preserved */
      var TABS = [
        { key: "assign", label: "Assign" },
        { key: "host", label: "Host" },
        { key: "map", label: "Map Setup", managerOnly: true }
      ];
      var tabsEl = htmlEl("nav", "floor-tabs");
      var badgeEls = {};
      function tabHref(key) {
        try {
          var url = new URL(window.location.href);
          var mock = url.searchParams.get("mock");
          url.search = "";
          url.searchParams.set("tab", key);
          if (mock) url.searchParams.set("mock", mock);
          return url.pathname + url.search;
        } catch (e) {
          return "?tab=" + key;
        }
      }
      TABS.forEach(function (tab) {
        if (tab.managerOnly && !opts.isManager) return;
        var a = htmlEl("a", "floor-tab");
        a.href = tabHref(tab.key);
        a.appendChild(document.createTextNode(tab.label));
        if (tab.key === opts.activeTab) a.classList.add("active");
        var badge = htmlEl("span", "floor-tab-badge");
        badge.hidden = true;
        a.appendChild(badge);
        badgeEls[tab.key] = badge;
        tabsEl.appendChild(a);
      });
      topbar.appendChild(tabsEl);

      /* --- service-area chips: All + areas --- */
      var areaRow = htmlEl("div", "floor-chiprow floor-arearow");
      var areas = [];
      function renderAreaChips() {
        areaRow.textContent = "";
        var all = [{ guid: null, name: "All" }].concat(areas);
        all.forEach(function (area) {
          var chip = htmlEl("button", "floor-chip", area.name);
          chip.type = "button";
          if ((area.guid || null) === currentArea) chip.classList.add("active");
          chip.addEventListener("click", function () {
            if (currentArea === (area.guid || null)) return;
            currentArea = area.guid || null;
            renderAreaChips();
            areaListeners.forEach(function (cb) { cb(currentArea); });
          });
          areaRow.appendChild(chip);
        });
      }
      renderAreaChips();

      /* --- server strip --- */
      var strip = htmlEl("div", "floor-serverstrip");

      /* --- canvas host --- */
      var canvasHost = htmlEl("div", "floor-canvas-host");

      shellEl.textContent = "";
      shellEl.appendChild(topbar);
      shellEl.appendChild(areaRow);
      shellEl.appendChild(strip);
      shellEl.appendChild(canvasHost);

      var shell = {
        canvasHost: canvasHost,
        panelEl: panelEl,
        attentionMinutes: opts.attentionMinutes,
        currentLoc: function () { return currentLoc; },
        currentArea: function () { return currentArea; },
        onLocationChange: function (cb) { if (typeof cb === "function") locListeners.push(cb); },
        onAreaChange: function (cb) { if (typeof cb === "function") areaListeners.push(cb); },
        setAreas: function (list) {
          areas = (list || []).slice();
          var stillThere = areas.some(function (a) { return a.guid === currentArea; });
          if (currentArea !== null && !stillThere) currentArea = null;
          renderAreaChips();
        },
        setServers: function (servers) {
          strip.textContent = "";
          (servers || []).forEach(function (s) {
            var chip = htmlEl("div", "floor-server-chip");
            chip.dataset.guid = s.guid || "";
            var av = htmlEl("span", "floor-avatar", s.initials || initials(s.name));
            if (s.color) av.style.background = s.color;
            var nameEl = htmlEl("span", "floor-server-name", s.name || "");
            var live = s.live || 0;
            var today = s.today || 0;
            /* EXACT cover-count pattern (frozen): "<live> live | <today> today" */
            var covers = htmlEl("span", "floor-server-covers", live + " live | " + today + " today");
            chip.appendChild(av);
            chip.appendChild(nameEl);
            chip.appendChild(covers);
            strip.appendChild(chip);
          });
        },
        setBadge: function (tabKey, n) {
          var badge = badgeEls[tabKey];
          if (!badge) return;
          if (!n) {
            badge.hidden = true;
            badge.textContent = "";
          } else {
            badge.hidden = false;
            badge.textContent = String(n);
          }
        }
      };
      return shell;
    }
  };

  /* ========================================================== Canvas ==== */

  /**
   * new FloorApp.Canvas(hostEl, {mode}) - mode 'view' | 'assign' | 'setup'.
   * Renders the floor as a responsive SVG (viewBox 0 0 1000 620) inside
   * hostEl. Pointer events (mouse + touch) drive tap / lasso / setup drag.
   */
  function Canvas(hostEl, opts) {
    opts = opts || {};
    this.mode = opts.mode || "view";
    this.promptLabel = opts.promptLabel || function (done) {
      var text = window.prompt("Label text (e.g. BAR, HOST, KITCHEN)", "");
      done(text == null ? "" : String(text).trim());
    };
    this.host = hostEl;
    this.tables = [];   /* registry: placed AND unplaced (x == null) */
    this.fixtures = [];
    this.states = {};   /* guid -> {status,color,initials,minutes,partySize} */
    this.areaFilter = null;
    this.selected = [];
    this._handlers = { tableTap: [], lassoSelect: [], change: [] };
    this._drawType = null;
    this._fixtureSeq = 1;

    var svg = el("svg", {
      viewBox: "0 0 " + FLOOR_W + " " + FLOOR_H,
      preserveAspectRatio: "xMidYMid meet",
      "class": "floor-canvas-svg"
    });
    this.svg = svg;
    this._gGrid = el("g", { "class": "floor-grid" });
    this._gFixtures = el("g", { "class": "floor-fixtures" });
    this._gTables = el("g", { "class": "floor-tables-g" });
    this._gOverlay = el("g", { "class": "floor-overlay" });
    svg.appendChild(this._gGrid);
    svg.appendChild(this._gFixtures);
    svg.appendChild(this._gTables);
    svg.appendChild(this._gOverlay);
    hostEl.appendChild(svg);

    this._renderGrid();
    this._bindPointer();
  }

  Canvas.prototype.on = function (name, cb) {
    if (this._handlers[name] && typeof cb === "function") {
      this._handlers[name].push(cb);
    }
    return this;
  };

  Canvas.prototype._emit = function (name, arg) {
    (this._handlers[name] || []).forEach(function (cb) { cb(arg); });
  };

  /* tables = floor rows merged w/ layout. Entries without x/y are treated
     as unplaced: never rendered here (setup tray is the page's job). */
  Canvas.prototype.setFloor = function (floor) {
    floor = floor || {};
    var self = this;
    this.tables = (floor.tables || []).map(function (t) {
      var placed = t.x != null && t.y != null;
      return {
        guid: t.guid,
        name: t.name || "",
        service_area_guid: t.service_area_guid || null,
        placed: placed,
        x: placed ? t.x : 0,
        y: placed ? t.y : 0,
        w: t.w != null ? t.w : 80,
        h: t.h != null ? t.h : 80,
        shape: t.shape || "square",
        rotation: t.rotation != null ? t.rotation : 0
      };
    });
    this.fixtures = (floor.fixtures || []).map(function (f) {
      return {
        _id: f.id != null ? "f" + f.id : "n" + (self._fixtureSeq++),
        type: f.type,
        x: f.x || 0,
        y: f.y || 0,
        w: f.w != null ? f.w : 120,
        h: f.h != null ? f.h : 20,
        rotation: f.rotation || 0,
        label: f.label || null
      };
    });
    this.selected = [];
    this.render();
  };

  /* {<table_guid>: {status:'open'|'occupied'|'attention', color, initials,
      minutes, partySize}} */
  Canvas.prototype.setTableStates = function (map) {
    this.states = map || {};
    this.render();
  };

  Canvas.prototype.filterArea = function (serviceAreaGuid) {
    this.areaFilter = serviceAreaGuid || null;
    this.render();
  };

  Canvas.prototype.setSelected = function (guids) {
    this.selected = (guids || []).slice();
    this.render();
  };

  Canvas.prototype.getSelected = function () {
    return this.selected.slice();
  };

  /* ---- setup-mode editing API ---- */

  Canvas.prototype.addTable = function (guid, shape, x, y) {
    var t = this._table(guid);
    if (!t) {
      t = { guid: guid, name: "", service_area_guid: null, placed: false,
            x: 0, y: 0, w: 80, h: 80, shape: "square", rotation: 0 };
      this.tables.push(t);
    }
    shape = shape || t.shape || "square";
    var w = shape === "rect" ? 140 : 80;
    var h = 80;
    t.shape = shape;
    t.rotation = shape === "diamond" ? 45 : 0;
    t.w = w;
    t.h = h;
    t.x = clamp(snap(x != null ? x : (FLOOR_W - w) / 2), 0, FLOOR_W - w);
    t.y = clamp(snap(y != null ? y : (FLOOR_H - h) / 2), 0, FLOOR_H - h);
    t.placed = true;
    this.selected = [guid];
    this.render();
    this._emit("change");
  };

  Canvas.prototype.getLayout = function () {
    return this.tables.filter(function (t) { return t.placed; }).map(function (t) {
      return { table_guid: t.guid, x: t.x, y: t.y, w: t.w, h: t.h,
               shape: t.shape, rotation: t.rotation };
    });
  };

  Canvas.prototype.getFixtures = function () {
    return this.fixtures.map(function (f) {
      return { type: f.type, x: f.x, y: f.y, w: f.w, h: f.h,
               rotation: f.rotation, label: f.label };
    });
  };

  Canvas.prototype.startFixtureDraw = function (type) {
    if (type !== "wall" && type !== "label") return;
    this._drawType = type;
    this.svg.style.cursor = "crosshair";
  };

  Canvas.prototype.cancelFixtureDraw = function () {
    this._drawType = null;
    this.svg.style.cursor = "";
  };

  Canvas.prototype.rotateSelected = function () {
    var self = this;
    var touched = false;
    this.selected.forEach(function (id) {
      var item = self._table(id) || self._fixture(id);
      if (item) {
        item.rotation = item.rotation === 45 ? 0 : 45; /* 45-degree toggle */
        touched = true;
      }
    });
    if (touched) {
      this.render();
      this._emit("change");
    }
  };

  Canvas.prototype.setShapeOfSelected = function (shape) {
    if (["square", "rect", "circle", "diamond"].indexOf(shape) === -1) return;
    var self = this;
    var touched = false;
    this.selected.forEach(function (guid) {
      var t = self._table(guid);
      if (!t) return;
      t.shape = shape;
      if (shape === "diamond") t.rotation = 45;       /* diamond = square @45 */
      else if (shape === "square") t.rotation = 0;
      if (shape !== "rect") {                          /* keep equal sides */
        var side = Math.max(t.w, t.h);
        t.w = side;
        t.h = side;
      }
      touched = true;
    });
    if (touched) {
      this.render();
      this._emit("change");
    }
  };

  Canvas.prototype.removeSelected = function () {
    var self = this;
    var touched = false;
    this.selected.forEach(function (id) {
      var t = self._table(id);
      if (t && t.placed) {       /* back to the tray */
        t.placed = false;
        touched = true;
        return;
      }
      var idx = self.fixtures.findIndex(function (f) { return f._id === id; });
      if (idx !== -1) {
        self.fixtures.splice(idx, 1);
        touched = true;
      }
    });
    if (touched) {
      this.selected = [];
      this.render();
      this._emit("change");
    }
  };

  /* Convert client (pointer) coords to canvas units. */
  Canvas.prototype.clientToCanvas = function (clientX, clientY) {
    var pt = this.svg.createSVGPoint();
    pt.x = clientX;
    pt.y = clientY;
    var ctm = this.svg.getScreenCTM();
    if (!ctm) return { x: 0, y: 0 };
    var p = pt.matrixTransform(ctm.inverse());
    return { x: p.x, y: p.y };
  };

  /* ---- internals ---- */

  Canvas.prototype._table = function (guid) {
    for (var i = 0; i < this.tables.length; i++) {
      if (this.tables[i].guid === guid) return this.tables[i];
    }
    return null;
  };

  Canvas.prototype._fixture = function (id) {
    for (var i = 0; i < this.fixtures.length; i++) {
      if (this.fixtures[i]._id === id) return this.fixtures[i];
    }
    return null;
  };

  Canvas.prototype._visibleTables = function () {
    var self = this;
    return this.tables.filter(function (t) {
      if (!t.placed) return false;
      if (self.areaFilter && t.service_area_guid !== self.areaFilter) return false;
      return true;
    });
  };

  Canvas.prototype._renderGrid = function () {
    var g = this._gGrid;
    g.textContent = "";
    g.appendChild(el("rect", { x: 0, y: 0, width: FLOOR_W, height: FLOOR_H, fill: "#2F3338" }));
    var stroke = "rgba(255,255,255,0.05)";
    for (var x = GRID_STEP; x < FLOOR_W; x += GRID_STEP) {
      g.appendChild(el("line", { x1: x, y1: 0, x2: x, y2: FLOOR_H, stroke: stroke, "stroke-width": 1 }));
    }
    for (var y = GRID_STEP; y < FLOOR_H; y += GRID_STEP) {
      g.appendChild(el("line", { x1: 0, y1: y, x2: FLOOR_W, y2: y, stroke: stroke, "stroke-width": 1 }));
    }
  };

  Canvas.prototype.render = function () {
    this._renderFixtures();
    this._renderTables();
  };

  Canvas.prototype._renderFixtures = function () {
    var g = this._gFixtures;
    g.textContent = "";
    var self = this;
    this.fixtures.forEach(function (f) {
      var cx = f.x + f.w / 2;
      var cy = f.y + f.h / 2;
      var grp = el("g", { "class": "floor-fixture", "data-fid": f._id });
      if (f.rotation) grp.setAttribute("transform", "rotate(" + f.rotation + " " + cx + " " + cy + ")");
      if (f.type === "wall") {
        /* light strip */
        grp.appendChild(el("rect", {
          x: f.x, y: f.y, width: f.w, height: f.h, rx: 3,
          fill: COLOR_WALL, opacity: 0.9
        }));
      } else {
        /* outlined box with centered label text */
        grp.appendChild(el("rect", {
          x: f.x, y: f.y, width: f.w, height: f.h, rx: 8,
          fill: "none", stroke: "rgba(255,255,255,0.45)", "stroke-width": 2
        }));
        var txt = el("text", {
          x: cx, y: cy,
          "text-anchor": "middle", "dominant-baseline": "central",
          fill: "rgba(255,255,255,0.75)",
          "font-size": Math.min(22, Math.max(12, f.h * 0.4)),
          "font-weight": 700, "letter-spacing": "2"
        }, f.label || "");
        if (f.rotation) txt.setAttribute("transform", "rotate(" + (-f.rotation) + " " + cx + " " + cy + ")");
        grp.appendChild(txt);
      }
      if (self.mode === "setup" && self.selected.indexOf(f._id) !== -1) {
        self._appendSelection(grp, f, f._id);
      }
      g.appendChild(grp);
    });
  };

  Canvas.prototype._renderTables = function () {
    var g = this._gTables;
    g.textContent = "";
    var self = this;
    this._visibleTables().forEach(function (t) {
      g.appendChild(self._tableNode(t));
    });
  };

  Canvas.prototype._tableNode = function (t) {
    var self = this;
    var state = this.states[t.guid] || { status: "open" };
    var status = state.status || "open";
    var cx = t.x + t.w / 2;
    var cy = t.y + t.h / 2;

    var fill, nameFill;
    if (status === "attention") {
      fill = COLOR_ATTENTION;
      nameFill = COLOR_NAME_DARK;
    } else if (status === "occupied") {
      fill = state.color || COLOR_UNKNOWN; /* unknown server = neutral */
      nameFill = "#FFFFFF";
    } else {
      fill = COLOR_OPEN_FILL;
      nameFill = COLOR_NAME_DARK;
    }

    var grp = el("g", { "class": "floor-table floor-table--" + status, "data-guid": t.guid });
    if (t.rotation) grp.setAttribute("transform", "rotate(" + t.rotation + " " + cx + " " + cy + ")");

    var shapeAttrs = { fill: fill, stroke: "rgba(0,0,0,0.25)", "stroke-width": 1 };
    if (t.shape === "circle") {
      shapeAttrs.cx = cx;
      shapeAttrs.cy = cy;
      shapeAttrs.r = t.w / 2;
      grp.appendChild(el("circle", shapeAttrs));
    } else {
      shapeAttrs.x = t.x;
      shapeAttrs.y = t.y;
      shapeAttrs.width = t.w;
      shapeAttrs.height = t.h;
      shapeAttrs.rx = 10;
      grp.appendChild(el("rect", shapeAttrs));
    }

    /* upright content: counter-rotate around the table center */
    var content = el("g", {});
    if (t.rotation) content.setAttribute("transform", "rotate(" + (-t.rotation) + " " + cx + " " + cy + ")");
    grp.appendChild(content);

    var occupied = status === "occupied" || status === "attention";
    if (occupied && state.initials) {
      /* circular initials chip above the table name */
      var chipR = 13;
      var chipCy = cy - 14;
      content.appendChild(el("circle", {
        cx: cx, cy: chipCy, r: chipR,
        fill: "#FFFFFF", stroke: "rgba(0,0,0,0.18)", "stroke-width": 1
      }));
      content.appendChild(el("text", {
        x: cx, y: chipCy,
        "text-anchor": "middle", "dominant-baseline": "central",
        fill: state.color || COLOR_UNKNOWN,
        "font-size": 11, "font-weight": 800
      }, state.initials));
      content.appendChild(el("text", {
        x: cx, y: cy + 12,
        "text-anchor": "middle", "dominant-baseline": "central",
        fill: nameFill, "font-size": 15, "font-weight": 700
      }, t.name));
    } else {
      content.appendChild(el("text", {
        x: cx, y: cy,
        "text-anchor": "middle", "dominant-baseline": "central",
        fill: nameFill, "font-size": 16, "font-weight": 700
      }, t.name));
    }

    if (occupied && state.minutes != null) {
      /* minutes badge, top-right corner (host uses it) */
      var bx = t.x + t.w - 4;
      var by = t.y + 4;
      var badge = el("g", { "class": "floor-minutes-badge" });
      if (t.rotation) badge.setAttribute("transform", "rotate(" + (-t.rotation) + " " + bx + " " + by + ")");
      var label = state.minutes + "m";
      var bw = Math.max(26, label.length * 8 + 8);
      badge.appendChild(el("rect", {
        x: bx - bw + 6, y: by - 9, width: bw, height: 18, rx: 9,
        fill: status === "attention" ? COLOR_NAME_DARK : "rgba(0,0,0,0.55)"
      }));
      badge.appendChild(el("text", {
        x: bx - bw / 2 + 6, y: by,
        "text-anchor": "middle", "dominant-baseline": "central",
        fill: status === "attention" ? COLOR_ATTENTION : "#FFFFFF",
        "font-size": 11, "font-weight": 700
      }, label));
      grp.appendChild(badge);
    }

    if (this.selected.indexOf(t.guid) !== -1) {
      this._appendSelection(grp, t, t.guid);
    }
    return grp;
  };

  /* selection ring (+ resize handles in setup mode) */
  Canvas.prototype._appendSelection = function (grp, item, id) {
    var pad = 5;
    grp.appendChild(el("rect", {
      x: item.x - pad, y: item.y - pad,
      width: item.w + pad * 2, height: item.h + pad * 2, rx: 12,
      fill: "none", stroke: COLOR_SELECT, "stroke-width": 3,
      "stroke-dasharray": "7 5", "class": "floor-selection-ring"
    }));
    if (this.mode !== "setup") return;
    var corners = [
      { k: "nw", x: item.x, y: item.y },
      { k: "ne", x: item.x + item.w, y: item.y },
      { k: "sw", x: item.x, y: item.y + item.h },
      { k: "se", x: item.x + item.w, y: item.y + item.h }
    ];
    corners.forEach(function (c) {
      grp.appendChild(el("rect", {
        x: c.x - 7, y: c.y - 7, width: 14, height: 14, rx: 3,
        fill: COLOR_SELECT, stroke: "#FFFFFF", "stroke-width": 1.5,
        "class": "floor-resize-handle", "data-handle": c.k, "data-target": id
      }));
    });
  };

  /* ---- pointer interaction (mouse + touch via pointer events) ---- */

  Canvas.prototype._bindPointer = function () {
    var self = this;
    var drag = null;

    function canvasPoint(ev) {
      return self.clientToCanvas(ev.clientX, ev.clientY);
    }

    this.svg.addEventListener("pointerdown", function (ev) {
      if (ev.button != null && ev.button !== 0) return;
      var p = canvasPoint(ev);
      var handleEl = ev.target.closest ? ev.target.closest("[data-handle]") : null;
      var tableEl = ev.target.closest ? ev.target.closest(".floor-table") : null;
      var fixEl = ev.target.closest ? ev.target.closest(".floor-fixture") : null;

      drag = {
        startClient: { x: ev.clientX, y: ev.clientY },
        start: p,
        moved: false,
        kind: "none"
      };

      if (self.mode === "setup" && self._drawType) {
        drag.kind = "draw";
        drag.rect = el("rect", {
          x: p.x, y: p.y, width: 0, height: 0, rx: 4,
          fill: "rgba(47,111,237,0.25)", stroke: COLOR_SELECT,
          "stroke-width": 2, "stroke-dasharray": "5 4"
        });
        self._gOverlay.appendChild(drag.rect);
      } else if (self.mode === "setup" && handleEl) {
        var targetId = handleEl.getAttribute("data-target");
        var item = self._table(targetId) || self._fixture(targetId);
        if (item) {
          drag.kind = "resize";
          drag.item = item;
          drag.corner = handleEl.getAttribute("data-handle");
          drag.orig = { x: item.x, y: item.y, w: item.w, h: item.h };
        }
      } else if (self.mode === "setup" && (tableEl || fixEl)) {
        var id = tableEl ? tableEl.getAttribute("data-guid") : fixEl.getAttribute("data-fid");
        var it = tableEl ? self._table(id) : self._fixture(id);
        if (it) {
          if (self.selected.length !== 1 || self.selected[0] !== id) {
            self.selected = [id];
            self.render();
          }
          drag.kind = "move";
          drag.id = id;
          drag.item = it;
          drag.isTable = !!tableEl;
          drag.orig = { x: it.x, y: it.y };
        }
      } else if (tableEl) {
        drag.kind = "tap-table";
        drag.id = tableEl.getAttribute("data-guid");
      } else if (self.mode === "assign") {
        drag.kind = "lasso";
        drag.rect = el("rect", {
          x: p.x, y: p.y, width: 0, height: 0, rx: 4,
          fill: "rgba(47,111,237,0.15)", stroke: COLOR_SELECT,
          "stroke-width": 2, "stroke-dasharray": "5 4"
        });
        self._gOverlay.appendChild(drag.rect);
      }

      if (drag.kind !== "none") {
        try { self.svg.setPointerCapture(ev.pointerId); } catch (e) { /* noop */ }
        ev.preventDefault();
      }
    });

    this.svg.addEventListener("pointermove", function (ev) {
      if (!drag) return;
      var dxClient = ev.clientX - drag.startClient.x;
      var dyClient = ev.clientY - drag.startClient.y;
      if (Math.abs(dxClient) > TAP_SLOP || Math.abs(dyClient) > TAP_SLOP) drag.moved = true;
      var p = canvasPoint(ev);

      if (drag.kind === "lasso" || drag.kind === "draw") {
        var x = Math.min(drag.start.x, p.x);
        var y = Math.min(drag.start.y, p.y);
        var w = Math.abs(p.x - drag.start.x);
        var h = Math.abs(p.y - drag.start.y);
        drag.rect.setAttribute("x", x);
        drag.rect.setAttribute("y", y);
        drag.rect.setAttribute("width", w);
        drag.rect.setAttribute("height", h);
      } else if (drag.kind === "move" && drag.moved) {
        var dx = p.x - drag.start.x;
        var dy = p.y - drag.start.y;
        drag.item.x = clamp(snap(drag.orig.x + dx), 0, FLOOR_W - drag.item.w);
        drag.item.y = clamp(snap(drag.orig.y + dy), 0, FLOOR_H - drag.item.h);
        drag.dirty = true;
        self.render();
      } else if (drag.kind === "resize") {
        self._applyResize(drag, p);
        drag.dirty = true;
        self.render();
      }
    });

    function finish(ev) {
      if (!drag) return;
      var d = drag;
      drag = null;
      try { self.svg.releasePointerCapture(ev.pointerId); } catch (e) { /* noop */ }

      if (d.rect && d.rect.parentNode) d.rect.parentNode.removeChild(d.rect);

      if (d.kind === "tap-table" && !d.moved) {
        self._emit("tableTap", d.id);
      } else if (d.kind === "move") {
        if (!d.moved) {
          self._emit("tableTap", d.id);
        } else if (d.dirty) {
          self._emit("change");
        }
      } else if (d.kind === "resize" && d.dirty) {
        self._emit("change");
      } else if (d.kind === "lasso") {
        if (d.moved) {
          var p = canvasPoint(ev);
          var rx = Math.min(d.start.x, p.x);
          var ry = Math.min(d.start.y, p.y);
          var rw = Math.abs(p.x - d.start.x);
          var rh = Math.abs(p.y - d.start.y);
          var hit = self._visibleTables().filter(function (t) {
            return t.x < rx + rw && t.x + t.w > rx && t.y < ry + rh && t.y + t.h > ry;
          }).map(function (t) { return t.guid; });
          self._emit("lassoSelect", hit);
        }
      } else if (d.kind === "draw") {
        var pe = canvasPoint(ev);
        var fx = snap(Math.min(d.start.x, pe.x));
        var fy = snap(Math.min(d.start.y, pe.y));
        var fw = Math.max(SNAP, snap(Math.abs(pe.x - d.start.x)));
        var fh = Math.max(SNAP, snap(Math.abs(pe.y - d.start.y)));
        var type = self._drawType;
        self.cancelFixtureDraw();
        if (d.moved) {
          if (type === "label") {
            self.promptLabel(function (text) {
              self._addFixture(type, fx, fy, fw, fh, text || "");
            });
          } else {
            self._addFixture(type, fx, fy, fw, fh, null);
          }
        }
      } else if (d.kind === "none" && !d.moved && self.mode === "setup") {
        if (self.selected.length) {
          self.selected = [];
          self.render();
        }
      }
    }

    this.svg.addEventListener("pointerup", finish);
    this.svg.addEventListener("pointercancel", function (ev) {
      if (drag && drag.rect && drag.rect.parentNode) drag.rect.parentNode.removeChild(drag.rect);
      drag = null;
      try { self.svg.releasePointerCapture(ev.pointerId); } catch (e) { /* noop */ }
    });
  };

  Canvas.prototype._addFixture = function (type, x, y, w, h, label) {
    var f = {
      _id: "n" + (this._fixtureSeq++),
      type: type,
      x: clamp(x, 0, FLOOR_W - SNAP),
      y: clamp(y, 0, FLOOR_H - SNAP),
      w: w, h: h, rotation: 0,
      label: type === "label" ? (label || "") : null
    };
    this.fixtures.push(f);
    this.selected = [f._id];
    this.render();
    this._emit("change");
  };

  Canvas.prototype._applyResize = function (drag, p) {
    var o = drag.orig;
    var it = drag.item;
    var minSide = 20;
    var x1 = o.x, y1 = o.y, x2 = o.x + o.w, y2 = o.y + o.h;
    if (drag.corner.indexOf("w") !== -1) x1 = p.x;
    if (drag.corner.indexOf("e") !== -1) x2 = p.x;
    if (drag.corner.indexOf("n") !== -1) y1 = p.y;
    if (drag.corner.indexOf("s") !== -1) y2 = p.y;
    var nx = snap(Math.min(x1, x2));
    var ny = snap(Math.min(y1, y2));
    var nw = Math.max(minSide, snap(Math.abs(x2 - x1)));
    var nh = Math.max(minSide, snap(Math.abs(y2 - y1)));
    var equalSides = it.shape === "square" || it.shape === "circle" || it.shape === "diamond";
    if (equalSides) {
      var side = Math.max(nw, nh);
      nw = side;
      nh = side;
    }
    it.x = clamp(nx, 0, FLOOR_W - nw);
    it.y = clamp(ny, 0, FLOOR_H - nh);
    it.w = nw;
    it.h = nh;
  };

  window.FloorApp = {
    PALETTE: PALETTE,
    initials: initials,
    Shell: Shell,
    Canvas: Canvas,
    FLOOR_W: FLOOR_W,
    FLOOR_H: FLOOR_H,
    SNAP: SNAP
  };
})();
