/* SA-5: Reservations + Waitlist + History panel (docs/floor_contract.md
 * sections 6, 8, 10). Exposes window.FloorReserve = { mount(panelEl, ctx) }.
 *
 * ctx = {
 *   loc()   -> current store slug ("uno"|"dos"),
 *   canvas  -> FloorApp.Canvas instance (READ-ONLY: we only .on('tableTap')),
 *   shell   -> FloorApp shell (unused here, reserved),
 *   api(path, opts) -> Promise(json)  -- ALL network goes through this; the
 *                      host owns mock/real routing (?mock=1 -> fixture).
 * }
 *
 * No process-local app state: everything we show is re-fetched from the API;
 * the only client state is view state (active sub-tab, date, search, pick mode).
 * Mount is re-entrant / idempotent: remounting tears down the previous
 * instance (its canvas listener goes dead, its timer is cleared).
 */
(function () {
  "use strict";

  var SUBTABS = ["reservations", "waitlist", "history"];

  var RESERVATION_TRANSITIONS = ["confirmed", "arrived", "no_show", "cancelled"];

  var PILL_LABELS = {
    upcoming: "Upcoming",
    confirmed: "Confirmed",
    arrived: "Arrived",
    seated: "Seated",
    no_show: "No-show",
    cancelled: "Cancelled",
    waiting: "Waiting",
    notified: "Notified",
    left: "Left"
  };

  /* Fallback skeleton - used only if the host hands us a panel element that
   * does not already contain the Jinja partial markup. Keep in sync with
   * app/templates/sections_reserve_panel.html. */
  var SKELETON =
    '<div id="floorReservePanel" class="floor-reserve floor-card">' +
    '<div class="floor-reserve__head">' +
    '<div class="floor-subtabs floor-reserve__subtabs" data-role="subtabs" role="tablist">' +
    '<button type="button" class="floor-subtab active" data-subtab="reservations" role="tab" aria-selected="true">Reservations <span class="floor-reserve__count" data-role="count-reservations"></span></button>' +
    '<button type="button" class="floor-subtab" data-subtab="waitlist" role="tab" aria-selected="false">Waitlist <span class="floor-reserve__count" data-role="count-waitlist"></span></button>' +
    '<button type="button" class="floor-subtab" data-subtab="history" role="tab" aria-selected="false">History</button>' +
    "</div>" +
    '<div class="floor-reserve__tools">' +
    '<input type="search" class="floor-input floor-search floor-reserve__search" data-role="search" placeholder="Search name or phone" aria-label="Search name or phone">' +
    '<div class="floor-datepager floor-reserve__datepager" data-role="datepager">' +
    '<button type="button" class="floor-btn floor-btn--ghost" data-role="date-prev" aria-label="Previous day">&lsaquo;</button>' +
    '<button type="button" class="floor-btn floor-btn--ghost" data-role="date-today">Today</button>' +
    '<button type="button" class="floor-btn floor-btn--ghost" data-role="date-next" aria-label="Next day">&rsaquo;</button>' +
    '<span class="floor-reserve__datelabel" data-role="date-label"></span>' +
    "</div></div>" +
    '<div class="floor-reserve__actions">' +
    '<button type="button" class="floor-btn floor-reserve__add" data-role="add-reservation">+ Add reservation</button>' +
    '<button type="button" class="floor-btn floor-btn--ghost floor-reserve__add-wait" data-role="add-waitlist">+ Add to waitlist</button>' +
    "</div></div>" +
    '<div class="floor-reserve__pickbar" data-role="pickbar" hidden><span data-role="pickbar-text">Tap an open table</span><button type="button" class="floor-btn floor-btn--ghost" data-role="pick-cancel">Cancel</button></div>' +
    '<div class="floor-reserve__notice" data-role="notice" hidden></div>' +
    '<div class="floor-reserve__body" data-role="list" aria-live="polite"><div class="floor-reserve__empty">Loading...</div></div>' +
    '<div class="floor-reserve__overlay" data-role="overlay" hidden><div class="floor-sheet floor-reserve__sheet" data-role="sheet"></div></div>' +
    "</div>";

  /* ----------------------------- utilities ----------------------------- */

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  /* Local business date as YYYY-MM-DD (browser local; the host stand is in
   * store timezone, matching the server's APP_TZ business-date semantics). */
  function localDateStr(d) {
    d = d || new Date();
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  function shiftDateStr(dateStr, deltaDays) {
    var p = dateStr.split("-");
    var d = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
    d.setDate(d.getDate() + deltaDays);
    return localDateStr(d);
  }

  function fmtTime(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  }

  function fmtDateLabel(dateStr) {
    if (dateStr === localDateStr()) return "Today";
    var p = dateStr.split("-");
    var d = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
    return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  }

  function minutesSince(iso) {
    if (!iso) return 0;
    var t = new Date(iso).getTime();
    if (isNaN(t)) return 0;
    return Math.max(0, Math.floor((Date.now() - t) / 60000));
  }

  function partyIcon() {
    return (
      '<svg class="floor-reserve__party-icon" viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">' +
      '<circle cx="8" cy="5" r="3" fill="currentColor"></circle>' +
      '<path d="M2 14c0-3 2.7-5 6-5s6 2 6 5z" fill="currentColor"></path>' +
      "</svg>"
    );
  }

  function pill(status) {
    return (
      '<span class="floor-pill floor-pill--' + esc(status) + '">' +
      esc(PILL_LABELS[status] || status) +
      "</span>"
    );
  }

  function matchesSearch(entry, q) {
    if (!q) return true;
    var hay = ((entry.guest_name || "") + " " + (entry.phone || "")).toLowerCase();
    return hay.indexOf(q.toLowerCase()) !== -1;
  }

  /* --------------------------- api helpers ----------------------------- */

  function apiGet(state, path) {
    return state.ctx.api(path, { method: "GET" });
  }

  function apiSend(state, path, method, payload) {
    return state.ctx.api(path, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
  }

  /* ------------------------------ mount -------------------------------- */

  function mount(panelEl, ctx) {
    if (!panelEl || !ctx || typeof ctx.api !== "function") {
      throw new Error("FloorReserve.mount(panelEl, ctx) requires a panel element and a ctx with api()");
    }

    // Tear down a previous instance (re-entrant mount).
    if (panelEl.__floorReserve && typeof panelEl.__floorReserve.destroy === "function") {
      panelEl.__floorReserve.destroy();
    }

    // Locate the partial's root; inject the fallback skeleton if absent.
    var root;
    if (panelEl.classList && panelEl.classList.contains("floor-reserve")) {
      root = panelEl;
    } else {
      root = panelEl.querySelector(".floor-reserve");
    }
    if (!root) {
      panelEl.insertAdjacentHTML("beforeend", SKELETON);
      root = panelEl.querySelector(".floor-reserve");
    }

    // Reset inner DOM to its pristine markup so re-mount drops every
    // previously-attached inner listener.
    if (root.__floorReservePristine == null) {
      root.__floorReservePristine = root.innerHTML;
    } else {
      root.innerHTML = root.__floorReservePristine;
    }

    var state = {
      ctx: ctx,
      root: root,
      dead: false,
      tab: "reservations",
      date: localDateStr(),
      search: "",
      pick: null, // {kind:'reservation'|'waitlist', entry} while picking a table
      historyDays: 1, // Gate 4 (ck): 1 = single day, 7 = "Last 7 days" toggle
      pendingReservation: null, // Gate 4 (ck): payload held across the duplicate confirm step
      data: { reservations: [], waitlist: [], history: null },
      timer: null,
      keyHandler: null,
      destroy: function () {
        state.dead = true;
        if (state.timer) {
          clearInterval(state.timer);
          state.timer = null;
        }
        if (state.keyHandler) {
          document.removeEventListener("keydown", state.keyHandler);
          state.keyHandler = null;
        }
      }
    };
    panelEl.__floorReserve = state;
    root.__floorReserve = state;

    wire(state);

    // Single canvas listener per mount; goes inert when this mount dies.
    if (ctx.canvas && typeof ctx.canvas.on === "function") {
      ctx.canvas.on("tableTap", function (guid) {
        if (state.dead || !state.pick) return;
        onPickedTable(state, guid);
      });
    }

    state.keyHandler = function (ev) {
      if (state.dead) return;
      if (ev.key === "Escape") {
        if (state.pick) exitPickMode(state);
        closeSheet(state);
      }
    };
    document.addEventListener("keydown", state.keyHandler);

    // Waitlist "waited-so-far" minutes tick (display only, no network).
    state.timer = setInterval(function () {
      if (state.dead) return;
      if (state.tab === "waitlist" && !state.pick) renderList(state);
    }, 30000);

    refreshAll(state);
    return state;
  }

  function $(state, sel) {
    return state.root.querySelector(sel);
  }

  function wire(state) {
    var tabs = state.root.querySelectorAll("[data-subtab]");
    Array.prototype.forEach.call(tabs, function (btn) {
      btn.addEventListener("click", function () {
        setTab(state, btn.getAttribute("data-subtab"));
      });
    });

    $(state, "[data-role=search]").addEventListener("input", function (ev) {
      state.search = ev.target.value || "";
      renderList(state);
    });

    $(state, "[data-role=date-prev]").addEventListener("click", function () {
      state.date = shiftDateStr(state.date, -1);
      refreshReservations(state);
    });
    $(state, "[data-role=date-next]").addEventListener("click", function () {
      state.date = shiftDateStr(state.date, 1);
      refreshReservations(state);
    });
    $(state, "[data-role=date-today]").addEventListener("click", function () {
      state.date = localDateStr();
      refreshReservations(state);
    });

    $(state, "[data-role=add-reservation]").addEventListener("click", function () {
      openAddSheet(state, "reservation");
    });
    $(state, "[data-role=add-waitlist]").addEventListener("click", function () {
      openAddSheet(state, "waitlist");
    });

    $(state, "[data-role=pick-cancel]").addEventListener("click", function () {
      exitPickMode(state);
    });

    $(state, "[data-role=overlay]").addEventListener("click", function (ev) {
      if (ev.target === $(state, "[data-role=overlay]")) closeSheet(state);
    });

    // Delegated row + sheet clicks.
    state.root.addEventListener("click", function (ev) {
      var t = ev.target.closest ? ev.target.closest("[data-action]") : null;
      if (!t || state.dead) return;
      handleAction(state, t);
    });
  }

  /* ---------------------------- data fetch ----------------------------- */

  function refreshAll(state) {
    refreshReservations(state);
    refreshWaitlist(state);
    if (state.tab === "history") refreshHistory(state);
  }

  function refreshReservations(state) {
    updateDateLabel(state);
    return apiGet(
      state,
      "/floor/api/reservations?loc=" + encodeURIComponent(state.ctx.loc()) +
        "&date=" + encodeURIComponent(state.date)
    )
      .then(function (json) {
        if (state.dead) return;
        state.data.reservations = (json && json.reservations) || [];
        updateCounts(state);
        if (state.tab === "reservations") renderList(state);
      })
      .catch(function (err) {
        showError(state, "Could not load reservations", err);
      });
  }

  function refreshWaitlist(state) {
    return apiGet(
      state,
      "/floor/api/waitlist?loc=" + encodeURIComponent(state.ctx.loc())
    )
      .then(function (json) {
        if (state.dead) return;
        state.data.waitlist = (json && json.waitlist) || [];
        updateCounts(state);
        if (state.tab === "waitlist") renderList(state);
      })
      .catch(function (err) {
        showError(state, "Could not load waitlist", err);
      });
  }

  function refreshHistory(state) {
    // Gate 4 (ck): days=N backfill view; param omitted for the single-day
    // default so the legacy response shape keeps flowing.
    return apiGet(
      state,
      "/floor/api/history?loc=" + encodeURIComponent(state.ctx.loc()) +
        (state.historyDays > 1 ? "&days=" + state.historyDays : "")
    )
      .then(function (json) {
        if (state.dead) return;
        state.data.history = json || { seatings: [], reservations: [], waitlist: [] };
        if (state.tab === "history") renderList(state);
      })
      .catch(function (err) {
        showError(state, "Could not load history", err);
      });
  }

  /* ------------------------------ render ------------------------------- */

  function setTab(state, tab) {
    if (SUBTABS.indexOf(tab) === -1) return;
    state.tab = tab;
    var tabs = state.root.querySelectorAll("[data-subtab]");
    Array.prototype.forEach.call(tabs, function (btn) {
      var active = btn.getAttribute("data-subtab") === tab;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    $(state, "[data-role=datepager]").style.display = tab === "reservations" ? "" : "none";
    if (tab === "history" && !state.data.history) {
      refreshHistory(state);
    }
    renderList(state);
  }

  function updateCounts(state) {
    // Live counts in the sub-tab labels: "Reservations (5)" / "Waitlist (3)".
    // Waitlist count = active entries (the endpoint's default waiting|notified set).
    $(state, "[data-role=count-reservations]").textContent =
      "(" + state.data.reservations.length + ")";
    $(state, "[data-role=count-waitlist]").textContent =
      "(" + state.data.waitlist.length + ")";
  }

  function updateDateLabel(state) {
    $(state, "[data-role=date-label]").textContent = fmtDateLabel(state.date);
  }

  function renderList(state) {
    var host = $(state, "[data-role=list]");
    if (state.tab === "reservations") {
      host.innerHTML = renderReservationRows(state);
    } else if (state.tab === "waitlist") {
      host.innerHTML = renderWaitlistRows(state);
    } else {
      host.innerHTML = renderHistory(state);
    }
  }

  function emptyState(text) {
    return '<div class="floor-reserve__empty">' + esc(text) + "</div>";
  }

  function renderReservationRows(state) {
    var rows = state.data.reservations.filter(function (r) {
      return matchesSearch(r, state.search);
    });
    if (!rows.length) {
      return emptyState(
        state.search
          ? "No reservations match your search."
          : "No reservations for " + fmtDateLabel(state.date).toLowerCase() + ". Tap + Add reservation to book one."
      );
    }
    return rows
      .map(function (r) {
        return (
          '<div class="floor-list-row floor-reserve__row" data-action="open-entry" data-kind="reservation" data-id="' + esc(r.id) + '">' +
          '<div class="floor-reserve__row-main">' +
          '<span class="floor-reserve__guest">' + esc(r.guest_name) + "</span>" +
          '<span class="floor-reserve__meta">' +
          '<span class="floor-reserve__party">' + partyIcon() + " " + esc(r.party_size) + "</span>" +
          '<span class="floor-reserve__time">' + esc(fmtTime(r.reserved_for)) + "</span>" +
          (r.notes ? '<span class="floor-reserve__notes">' + esc(r.notes) + "</span>" : "") +
          (r.seating_id ? '<span class="floor-reserve__link">seating #' + esc(r.seating_id) + "</span>" : "") +
          "</span>" +
          "</div>" +
          pill(r.status) +
          "</div>"
        );
      })
      .join("");
  }

  function renderWaitlistRows(state) {
    var rows = state.data.waitlist.filter(function (w) {
      return matchesSearch(w, state.search);
    });
    if (!rows.length) {
      return emptyState(
        state.search ? "No waitlist entries match your search." : "Waitlist is empty."
      );
    }
    return rows
      .map(function (w) {
        var waited = minutesSince(w.joined_at);
        var quoted = w.quoted_minutes != null ? w.quoted_minutes : null;
        var over = quoted != null && waited > quoted;
        return (
          '<div class="floor-list-row floor-reserve__row" data-action="open-entry" data-kind="waitlist" data-id="' + esc(w.id) + '">' +
          '<div class="floor-reserve__row-main">' +
          '<span class="floor-reserve__guest">' + esc(w.guest_name) + "</span>" +
          '<span class="floor-reserve__meta">' +
          '<span class="floor-reserve__party">' + partyIcon() + " " + esc(w.party_size) + "</span>" +
          (quoted != null ? '<span class="floor-reserve__quoted">quoted ' + esc(quoted) + "m</span>" : "") +
          '<span class="floor-reserve__waited' + (over ? " floor-reserve__waited--over" : "") + '">waited ' + waited + "m</span>" +
          (w.seating_id ? '<span class="floor-reserve__link">seating #' + esc(w.seating_id) + "</span>" : "") +
          "</span>" +
          "</div>" +
          pill(w.status) +
          "</div>"
        );
      })
      .join("");
  }

  /* Gate 4 (ck): "Last 7 days" toggle markup (reuses existing classes). */
  function historyDaysToggle(state) {
    return (
      '<div class="floor-reserve__actions">' +
      '<button type="button" class="floor-btn floor-btn--ghost" data-action="toggle-history-days" data-role="history-days-toggle">' +
      (state.historyDays > 1 ? "Today only" : "Last 7 days") +
      "</button></div>"
    );
  }

  function renderHistory(state) {
    var h = state.data.history;
    if (!h) return historyDaysToggle(state) + emptyState("Loading history...");
    /* Gate 4 (ck): days>1 payload = {ok, days:[{date, ...groups}]}; render
       one dated section per bucket. Single-day payload keeps the old path. */
    if (h.days) {
      return (
        historyDaysToggle(state) +
        h.days
          .map(function (bucket) {
            return (
              '<div class="floor-reserve__group-title">' +
              esc(fmtDateLabel(bucket.date)) +
              "</div>" +
              renderHistoryGroups(state, bucket)
            );
          })
          .join("")
      );
    }
    return historyDaysToggle(state) + renderHistoryGroups(state, h);
  }

  function renderHistoryGroups(state, h) {
    var q = state.search;
    var seatings = (h.seatings || []).filter(function (s) {
      if (!q) return true;
      var hay = ((s.server_name || "") + " " + (s.table_name || "")).toLowerCase();
      return hay.indexOf(q.toLowerCase()) !== -1;
    });
    var resv = (h.reservations || []).filter(function (r) {
      return matchesSearch(r, q);
    });
    var wait = (h.waitlist || []).filter(function (w) {
      return matchesSearch(w, q);
    });

    var out = "";

    out += '<div class="floor-reserve__group-title">Seatings</div>';
    out += seatings.length
      ? seatings
          .map(function (s) {
            return (
              '<div class="floor-list-row floor-reserve__row floor-reserve__row--history">' +
              '<div class="floor-reserve__row-main">' +
              '<span class="floor-reserve__guest">Table ' + esc(s.table_name || s.table_guid) + "</span>" +
              '<span class="floor-reserve__meta">' +
              '<span class="floor-reserve__party">' + partyIcon() + " " + esc(s.party_size) + "</span>" +
              (s.server_name ? '<span class="floor-reserve__server">' + esc(s.server_name) + "</span>" : "") +
              '<span class="floor-reserve__time">' + esc(fmtTime(s.seated_at)) +
              (s.cleared_at ? " - " + esc(fmtTime(s.cleared_at)) : " (open)") +
              "</span>" +
              "</span>" +
              "</div>" +
              (s.cleared_at
                ? '<span class="floor-pill floor-pill--cleared">Cleared</span>'
                : pill("seated")) +
              "</div>"
            );
          })
          .join("")
      : emptyState("No seatings yet.");

    out += '<div class="floor-reserve__group-title">Reservations (final)</div>';
    out += resv.length
      ? resv
          .map(function (r) {
            return (
              '<div class="floor-list-row floor-reserve__row floor-reserve__row--history">' +
              '<div class="floor-reserve__row-main">' +
              '<span class="floor-reserve__guest">' + esc(r.guest_name) + "</span>" +
              '<span class="floor-reserve__meta">' +
              '<span class="floor-reserve__party">' + partyIcon() + " " + esc(r.party_size) + "</span>" +
              '<span class="floor-reserve__time">' + esc(fmtTime(r.reserved_for)) + "</span>" +
              (r.seating_id ? '<span class="floor-reserve__link">seating #' + esc(r.seating_id) + "</span>" : "") +
              "</span>" +
              "</div>" +
              pill(r.status) +
              "</div>"
            );
          })
          .join("")
      : emptyState("No finished reservations.");

    out += '<div class="floor-reserve__group-title">Waitlist (final)</div>';
    out += wait.length
      ? wait
          .map(function (w) {
            return (
              '<div class="floor-list-row floor-reserve__row floor-reserve__row--history">' +
              '<div class="floor-reserve__row-main">' +
              '<span class="floor-reserve__guest">' + esc(w.guest_name) + "</span>" +
              '<span class="floor-reserve__meta">' +
              '<span class="floor-reserve__party">' + partyIcon() + " " + esc(w.party_size) + "</span>" +
              (w.quoted_minutes != null ? '<span class="floor-reserve__quoted">quoted ' + esc(w.quoted_minutes) + "m</span>" : "") +
              (w.seating_id ? '<span class="floor-reserve__link">seating #' + esc(w.seating_id) + "</span>" : "") +
              "</span>" +
              "</div>" +
              pill(w.status) +
              "</div>"
            );
          })
          .join("")
      : emptyState("No finished waitlist entries.");

    return out;
  }

  /* --------------------------- notices/errors -------------------------- */

  function showNotice(state, text) {
    var n = $(state, "[data-role=notice]");
    n.textContent = text;
    n.hidden = false;
    setTimeout(function () {
      if (!state.dead) n.hidden = true;
    }, 4000);
  }

  function showError(state, text, err) {
    if (state.dead) return;
    var detail = err && err.error ? " (" + err.error + ")" : "";
    showNotice(state, text + detail);
  }

  /* ------------------------------ sheets ------------------------------- */

  function openSheet(state, html) {
    $(state, "[data-role=sheet]").innerHTML = html;
    $(state, "[data-role=overlay]").hidden = false;
  }

  function closeSheet(state) {
    state.pendingReservation = null; // Gate 4 (ck): drop any held duplicate payload
    $(state, "[data-role=overlay]").hidden = true;
    $(state, "[data-role=sheet]").innerHTML = "";
  }

  function stepperHtml(value) {
    return (
      '<div class="floor-stepper floor-reserve__stepper" data-role="stepper">' +
      '<button type="button" class="floor-btn floor-btn--ghost" data-action="step-down" aria-label="Fewer guests">-</button>' +
      '<span class="floor-reserve__stepper-value" data-role="stepper-value">' + esc(value) + "</span>" +
      '<button type="button" class="floor-btn floor-btn--ghost" data-action="step-up" aria-label="More guests">+</button>' +
      "</div>"
    );
  }

  function openAddSheet(state, kind) {
    var isResv = kind === "reservation";
    var now = new Date();
    now.setMinutes(now.getMinutes() + 30 - (now.getMinutes() % 15), 0, 0);
    var defTime = pad2(now.getHours()) + ":" + pad2(now.getMinutes());
    var html =
      '<div class="floor-reserve__sheet-title">' + (isResv ? "New reservation" : "Add to waitlist") + "</div>" +
      '<label class="floor-reserve__field">Guest name' +
      '<input type="text" class="floor-input" data-role="f-guest" placeholder="Guest name" required></label>' +
      '<label class="floor-reserve__field">Phone' +
      '<input type="tel" class="floor-input" data-role="f-phone" placeholder="555-0100"></label>' +
      '<div class="floor-reserve__field"><span>Party size</span>' + stepperHtml(2) + "</div>" +
      (isResv
        ? '<div class="floor-reserve__field-row">' +
          '<label class="floor-reserve__field">Date' +
          '<input type="date" class="floor-input" data-role="f-date" value="' + esc(state.date) + '"></label>' +
          '<label class="floor-reserve__field">Time' +
          '<input type="time" class="floor-input" data-role="f-time" value="' + esc(defTime) + '"></label>' +
          "</div>" +
          '<label class="floor-reserve__field">Notes' +
          '<textarea class="floor-input" data-role="f-notes" rows="2" placeholder="Allergies, occasion..."></textarea></label>'
        : '<label class="floor-reserve__field">Quoted wait (minutes)' +
          '<input type="number" class="floor-input" data-role="f-quoted" min="0" step="5" value="15"></label>') +
      '<div class="floor-reserve__sheet-actions">' +
      '<button type="button" class="floor-btn floor-btn--ghost" data-action="sheet-cancel">Cancel</button>' +
      '<button type="button" class="floor-btn" data-action="' + (isResv ? "create-reservation" : "create-waitlist") + '">' +
      (isResv ? "Add reservation" : "Add to waitlist") +
      "</button></div>";
    openSheet(state, html);
    var g = $(state, "[data-role=f-guest]");
    if (g) g.focus();
  }

  function openEntrySheet(state, kind, entry) {
    var isResv = kind === "reservation";
    var terminal = isResv
      ? entry.status === "seated" || entry.status === "no_show" || entry.status === "cancelled"
      : entry.status === "seated" || entry.status === "left";

    var meta = isResv
      ? fmtTime(entry.reserved_for) + " - party of " + entry.party_size +
        (entry.phone ? " - " + entry.phone : "")
      : "party of " + entry.party_size +
        (entry.quoted_minutes != null ? " - quoted " + entry.quoted_minutes + "m" : "") +
        " - waited " + minutesSince(entry.joined_at) + "m" +
        (entry.phone ? " - " + entry.phone : "");

    var html =
      '<div class="floor-reserve__sheet-title">' + esc(entry.guest_name) + " " + pill(entry.status) + "</div>" +
      '<div class="floor-reserve__sheet-meta">' + esc(meta) + "</div>" +
      (entry.notes ? '<div class="floor-reserve__sheet-meta">' + esc(entry.notes) + "</div>" : "") +
      (entry.seating_id
        ? '<div class="floor-reserve__sheet-meta">Linked to seating #' + esc(entry.seating_id) + "</div>"
        : "");

    if (!terminal) {
      html +=
        '<button type="button" class="floor-btn floor-reserve__seat-btn" data-action="start-seat" data-kind="' +
        esc(kind) + '" data-id="' + esc(entry.id) + '">Seat</button>';
      html += '<div class="floor-reserve__sheet-group">';
      if (isResv) {
        RESERVATION_TRANSITIONS.forEach(function (s) {
          if (s === entry.status) return;
          html +=
            '<button type="button" class="floor-btn floor-btn--ghost" data-action="set-status" data-kind="reservation" data-id="' +
            esc(entry.id) + '" data-status="' + esc(s) + '">' + esc(PILL_LABELS[s]) + "</button>";
        });
      } else {
        // 'notified' is a MANUAL toggle this run - plain status write, SMS out of scope.
        var notifyTo = entry.status === "notified" ? "waiting" : "notified";
        html +=
          '<button type="button" class="floor-btn floor-btn--ghost" data-action="set-status" data-kind="waitlist" data-id="' +
          esc(entry.id) + '" data-status="' + esc(notifyTo) + '">' +
          (entry.status === "notified" ? "Back to waiting" : "Mark notified") +
          "</button>";
        html +=
          '<button type="button" class="floor-btn floor-btn--ghost" data-action="set-status" data-kind="waitlist" data-id="' +
          esc(entry.id) + '" data-status="left">Left</button>';
      }
      html += "</div>";
    }

    html +=
      '<div class="floor-reserve__sheet-actions">' +
      '<button type="button" class="floor-btn floor-btn--ghost" data-action="sheet-cancel">Close</button>' +
      "</div>";

    openSheet(state, html);
  }

  /* ------------------------------ actions ------------------------------ */

  function findEntry(state, kind, id) {
    var list = kind === "reservation" ? state.data.reservations : state.data.waitlist;
    for (var i = 0; i < list.length; i++) {
      if (String(list[i].id) === String(id)) return list[i];
    }
    return null;
  }

  function handleAction(state, el) {
    var action = el.getAttribute("data-action");
    var kind = el.getAttribute("data-kind");
    var id = el.getAttribute("data-id");

    if (action === "open-entry") {
      var entry = findEntry(state, kind, id);
      if (entry) openEntrySheet(state, kind, entry);
    } else if (action === "sheet-cancel") {
      closeSheet(state);
    } else if (action === "step-down" || action === "step-up") {
      var v = $(state, "[data-role=stepper-value]");
      var n = parseInt(v.textContent, 10) || 1;
      n = action === "step-up" ? n + 1 : Math.max(1, n - 1);
      v.textContent = String(n);
    } else if (action === "create-reservation") {
      submitNewReservation(state);
    } else if (action === "create-waitlist") {
      submitNewWaitlist(state);
    } else if (action === "set-status") {
      patchStatus(state, kind, id, el.getAttribute("data-status"));
    } else if (action === "start-seat") {
      var seatEntry = findEntry(state, kind, id);
      if (seatEntry) enterPickMode(state, kind, seatEntry);
    } else if (action === "confirm-duplicate") {
      // Gate 4 (ck): resend the held payload with confirm:true.
      var dupPayload = state.pendingReservation;
      state.pendingReservation = null;
      if (dupPayload) {
        dupPayload.confirm = true;
        sendReservation(state, dupPayload);
      } else {
        closeSheet(state);
      }
    } else if (action === "toggle-history-days") {
      // Gate 4 (ck): flip History between today-only and last 7 days.
      state.historyDays = state.historyDays > 1 ? 1 : 7;
      state.data.history = null;
      renderList(state);
      refreshHistory(state);
    }
  }

  function submitNewReservation(state) {
    var guest = ($(state, "[data-role=f-guest]").value || "").trim();
    var phone = ($(state, "[data-role=f-phone]").value || "").trim();
    var party = parseInt($(state, "[data-role=stepper-value]").textContent, 10) || 1;
    var date = $(state, "[data-role=f-date]").value || state.date;
    var time = $(state, "[data-role=f-time]").value || "18:00";
    var notes = ($(state, "[data-role=f-notes]").value || "").trim();
    if (!guest) {
      showNotice(state, "Guest name is required.");
      return;
    }
    // Local-naive ISO datetime; the server interprets it in APP_TZ (contract sec 6 #12).
    var reservedFor = date + "T" + time + ":00";
    sendReservation(state, {
      loc: state.ctx.loc(),
      guest_name: guest,
      phone: phone,
      party_size: party,
      reserved_for: reservedFor,
      notes: notes
    });
  }

  /* Gate 4 (ck): shared POST path for new reservations. The first pass may
     bounce 409 {"error":"duplicate"} (same phone within +/-90 min); the
     confirm step resends the SAME payload with confirm:true. */
  function sendReservation(state, payload) {
    apiSend(state, "/floor/api/reservations", "POST", payload)
      .then(function (json) {
        if (state.dead) return;
        if (json && json.ok === false) {
          if (json.duplicate || json.error === "duplicate") {
            openDuplicateConfirmSheet(state, payload);
            return;
          }
          showError(state, "Could not add reservation", json);
          return;
        }
        closeSheet(state);
        showNotice(state, "Reservation added for " + payload.guest_name + ".");
        state.date = (payload.reserved_for || "").slice(0, 10) || state.date;
        refreshReservations(state);
        setTab(state, "reservations");
      })
      .catch(function (err) {
        showError(state, "Could not add reservation", err);
      });
  }

  /* Gate 4 (ck): duplicate-guest confirm step (contract section 12). */
  function openDuplicateConfirmSheet(state, payload) {
    state.pendingReservation = payload;
    openSheet(
      state,
      '<div class="floor-reserve__sheet-title">Duplicate booking?</div>' +
        '<div class="floor-reserve__sheet-meta">Looks like a duplicate booking - add anyway?</div>' +
        '<div class="floor-reserve__sheet-actions">' +
        '<button type="button" class="floor-btn floor-btn--ghost" data-action="sheet-cancel">Cancel</button>' +
        '<button type="button" class="floor-btn" data-action="confirm-duplicate">Add anyway</button>' +
        "</div>"
    );
  }

  function submitNewWaitlist(state) {
    var guest = ($(state, "[data-role=f-guest]").value || "").trim();
    var phone = ($(state, "[data-role=f-phone]").value || "").trim();
    var party = parseInt($(state, "[data-role=stepper-value]").textContent, 10) || 1;
    var quotedRaw = $(state, "[data-role=f-quoted]").value;
    var quoted = quotedRaw === "" ? null : parseInt(quotedRaw, 10);
    if (!guest) {
      showNotice(state, "Guest name is required.");
      return;
    }
    var payload = {
      loc: state.ctx.loc(),
      guest_name: guest,
      phone: phone,
      party_size: party
    };
    if (quoted != null && !isNaN(quoted)) payload.quoted_minutes = quoted;
    apiSend(state, "/floor/api/waitlist", "POST", payload)
      .then(function (json) {
        if (state.dead) return;
        if (json && json.ok === false) {
          showError(state, "Could not add to waitlist", json);
          return;
        }
        closeSheet(state);
        showNotice(state, guest + " added to waitlist.");
        refreshWaitlist(state);
        setTab(state, "waitlist");
      })
      .catch(function (err) {
        showError(state, "Could not add to waitlist", err);
      });
  }

  function patchStatus(state, kind, id, status) {
    var path =
      kind === "reservation"
        ? "/floor/api/reservations/" + encodeURIComponent(id)
        : "/floor/api/waitlist/" + encodeURIComponent(id);
    apiSend(state, path, "PATCH", { status: status })
      .then(function (json) {
        if (state.dead) return;
        if (json && json.ok === false) {
          showError(state, "Could not update status", json);
          return;
        }
        closeSheet(state);
        if (kind === "reservation") refreshReservations(state);
        else refreshWaitlist(state);
        if (state.data.history) refreshHistory(state);
      })
      .catch(function (err) {
        showError(state, "Could not update status", err);
      });
  }

  /* --------------------------- seat (pick mode) ------------------------ */

  function enterPickMode(state, kind, entry) {
    closeSheet(state);
    state.pick = { kind: kind, entry: entry };
    var bar = $(state, "[data-role=pickbar]");
    $(state, "[data-role=pickbar-text]").textContent =
      "Tap an open table to seat " + entry.guest_name + " (party of " + entry.party_size + ")";
    bar.hidden = false;
    state.root.classList.add("floor-reserve--picking");
  }

  function exitPickMode(state) {
    state.pick = null;
    $(state, "[data-role=pickbar]").hidden = true;
    state.root.classList.remove("floor-reserve--picking");
  }

  function onPickedTable(state, tableGuid) {
    var pick = state.pick;
    if (!pick) return;
    exitPickMode(state); // one-shot: consume the tap immediately
    var payload = {
      loc: state.ctx.loc(),
      table_guid: tableGuid,
      party_size: pick.entry.party_size // explicit, though the link carries it server-side
    };
    if (pick.kind === "reservation") payload.reservation_id = pick.entry.id;
    else payload.waitlist_id = pick.entry.id;

    apiSend(state, "/floor/api/seat", "POST", payload)
      .then(function (json) {
        if (state.dead) return;
        if (json && json.ok === false) {
          showError(
            state,
            json.error === "occupied" ? "That table is occupied - pick another." : "Could not seat party",
            json
          );
          if (json.error === "occupied") enterPickMode(state, pick.kind, pick.entry);
          return;
        }
        showNotice(state, pick.entry.guest_name + " seated.");
        refreshReservations(state);
        refreshWaitlist(state);
        if (state.data.history) refreshHistory(state);
      })
      .catch(function (err) {
        showError(state, "Could not seat party", err);
      });
  }

  window.FloorReserve = { mount: mount };
})();
