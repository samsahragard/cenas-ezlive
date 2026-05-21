/* ============================================================
   sidebar.js — Cenas Kitchen sidebar behavior

   Desktop:
     • Toggle sections AND nested branches; persist collapsed state.
     • Auto-light ancestor branches of the active leaf.

   Mobile (< 1024px) and Capacitor:
     • Hamburger opens the drawer.
     • Backdrop or X closes it.
     • Tapping any leaf link closes it (then navigates).
     • Swipe left on the panel closes it.
     • Android hardware back button closes it (intercepted before navigation).
   ============================================================ */
(function () {
  'use strict';

  var STORAGE_KEY = 'ck-sidebar-collapsed';
  var MOBILE_BP = 1024;

  function loadState() {
    try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); }
    catch (e) { return {}; }
  }
  function saveState(state) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); }
    catch (e) { /* fail silently */ }
  }

  function isMobile() {
    return window.matchMedia('(max-width: ' + (MOBILE_BP - 1) + 'px)').matches;
  }

  /* ---------- Collapsible section/branch wiring ---------- */
  function wireToggle(parent, head, state) {
    var id = parent.dataset.id;

    if (id && state[id]) {
      parent.classList.add('collapsed');
      head.setAttribute('aria-expanded', 'false');
    }

    head.addEventListener('click', function (e) {
      e.stopPropagation();
      parent.classList.toggle('collapsed');
      var collapsed = parent.classList.contains('collapsed');
      head.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      if (id) {
        state[id] = collapsed;
        saveState(state);
      }
    });
  }

  /* ---------- Mobile drawer ---------- */
  function setupDrawer() {
    var host = document.querySelector('.ck-sidebar-host');
    if (!host) return null;

    var trigger = host.querySelector('.ck-mobile-trigger');
    var backdrop = host.querySelector('.ck-mobile-backdrop');
    var closeBtn = host.querySelector('.ck-mobile-close');
    var sidebar = host.querySelector('.ck-sidebar');

    function open() {
      host.dataset.open = 'true';
      document.body.classList.add('ck-drawer-open');
      if (trigger) trigger.setAttribute('aria-expanded', 'true');
      if (sidebar) sidebar.setAttribute('aria-hidden', 'false');
    }

    function close() {
      host.dataset.open = 'false';
      document.body.classList.remove('ck-drawer-open');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
      if (sidebar) sidebar.setAttribute('aria-hidden', 'true');
    }

    function isOpen() { return host.dataset.open === 'true'; }

    if (trigger) trigger.addEventListener('click', open);
    if (backdrop) backdrop.addEventListener('click', close);
    if (closeBtn) closeBtn.addEventListener('click', close);

    // Tapping a leaf link closes the drawer (then native navigation proceeds)
    host.querySelectorAll('a.ck-sb-item, a.ck-sb-subitem, a.ck-sb-logout').forEach(function (link) {
      link.addEventListener('click', function () {
        if (isMobile() && isOpen()) close();
      });
    });

    // Escape key closes the drawer (keyboard accessibility)
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isOpen()) close();
    });

    // Swipe left on the panel to close
    var touchStartX = null;
    var touchStartY = null;
    var dragging = false;

    if (sidebar) {
      sidebar.addEventListener('touchstart', function (e) {
        if (!isMobile() || !isOpen()) return;
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
        dragging = false;
      }, { passive: true });

      sidebar.addEventListener('touchmove', function (e) {
        if (touchStartX === null) return;
        var dx = e.touches[0].clientX - touchStartX;
        var dy = e.touches[0].clientY - touchStartY;

        // Only treat as horizontal swipe if x-movement dominates
        if (!dragging && Math.abs(dx) > 10 && Math.abs(dx) > Math.abs(dy)) {
          dragging = true;
        }
        if (dragging && dx < 0) {
          sidebar.style.transform = 'translateX(' + Math.max(dx, -320) + 'px)';
          sidebar.style.transition = 'none';
        }
      }, { passive: true });

      sidebar.addEventListener('touchend', function (e) {
        if (touchStartX === null) return;
        var dx = e.changedTouches[0].clientX - touchStartX;
        sidebar.style.transform = '';
        sidebar.style.transition = '';
        if (dragging && dx < -60) close();
        touchStartX = null;
        touchStartY = null;
        dragging = false;
      });
    }

    // Android hardware back button (Capacitor App plugin)
    if (window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.App) {
      window.Capacitor.Plugins.App.addListener('backButton', function () {
        if (isOpen()) {
          close();
          return;
        }
        // Hardware back -> a plain in-app history back. history.back()
        // navigates back when there is history and is a harmless no-op
        // at the first entry — it never exits the app. The website-side
        // guards (base_dashboard entry-page guard, bottom-nav menu
        // guard) pick it up via popstate. We deliberately do NOT call
        // exitApp(): the old window.history.length check was unreliable
        // for a remote-loaded Capacitor shell and spuriously exited the
        // app (Sam: the back button kept closing the app). #39.
        window.history.back();
      });
    }
    // Cordova-style fallback (older Capacitor versions or hybrid setups)
    document.addEventListener('backbutton', function (e) {
      if (isOpen()) {
        e.preventDefault();
        close();
      }
    }, false);

    // Reset drawer state if user resizes from mobile to desktop while open
    window.addEventListener('resize', function () {
      if (!isMobile() && isOpen()) close();
    });

    return { open: open, close: close, isOpen: isOpen };
  }

  /* ---------- Init ---------- */
  function init() {
    var state = loadState();

    document.querySelectorAll('.ck-sb-section').forEach(function (section) {
      var head = section.querySelector(':scope > .ck-sb-section-head');
      if (head) wireToggle(section, head, state);
    });

    document.querySelectorAll('.ck-sb-branch').forEach(function (branch) {
      var head = branch.querySelector(':scope > .ck-sb-branch-head');
      if (head) wireToggle(branch, head, state);
    });

    document.querySelectorAll('.ck-sb-subitem.active, .ck-sb-item.active').forEach(function (leaf) {
      var branch = leaf.closest('.ck-sb-branch');
      while (branch) {
        branch.classList.add('has-active');
        var parent = branch.parentElement;
        branch = parent ? parent.closest('.ck-sb-branch') : null;
      }
    });

    // Mobile drawer setup (no-op if .ck-sidebar-host missing)
    var drawer = setupDrawer();

    // Expose for external use if needed (e.g., closing from elsewhere in the app)
    if (drawer) window.ckSidebar = drawer;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
