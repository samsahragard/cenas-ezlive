/* Universal ribbon — X / Check behavior. Phase 2 / Block 1D (ck, 2026-05-14).
 *
 * 1B rendered the X / Check control MARKUP (inert); 1D makes it live.
 * This file delegates clicks on the [data-action] buttons inside
 * .ribbon-item elements, POSTs to the 1D dismiss/check endpoints, and
 * removes the acted-on item(s) from the DOM on success.
 *
 * The §6.2 markup contract 1B shipped (do not change without re-speccing):
 *   <div class="ribbon-item ..." data-item-type="..." data-item-id="...">
 *     <span class="ribbon-item__text">...</span>
 *     <button class="ribbon-item__x"     data-action="dismiss">×</button>
 *     <button class="ribbon-item__check" data-action="check">✓</button>
 *   </div>
 *
 * Collapse-toggle behavior is NOT here — it's the inline <script> in
 * _ribbon.html (1B owns it; it's a 1B concern). Consolidating both into
 * this file is a clean-up candidate, deliberately out of 1D scope.
 *
 * One-source-two-items (aick's 1C contract): a task can render as two
 * .ribbon-item elements with the SAME data-item-type + data-item-id
 * (owner_todo in `todo` + observer in its domain category). A dismiss
 * or check acts on the underlying ROW, so on success we remove EVERY
 * matching .ribbon-item, not just the clicked one — mirroring 1C's
 * (item_type,item_id)-keyed exclusion on the next server render.
 */
(function () {
  var ribbon = document.querySelector('.ck-ribbon');
  if (!ribbon) return;  // ribbon not on this page — nothing to wire.

  // Re-point the count badge after items are removed — or, if the
  // category is now empty, remove the whole category block.
  //
  // 1B Refinement-1 (1C spec amendment #1394): a zero-item category is
  // zero DOM. The server skips empty categories entirely on render, so
  // when the last item in a category is removed client-side we remove
  // the whole .ribbon-category block to match — no empty-state line,
  // no orphaned header. This keeps the post-dismiss DOM identical to
  // what a fresh server render would produce.
  function refreshCategory(categoryEl) {
    if (!categoryEl) return;
    var body = categoryEl.querySelector('.ribbon-category__body');
    if (!body) return;
    var items = body.querySelectorAll('.ribbon-item');
    if (items.length === 0) {
      categoryEl.remove();
      return;
    }
    var countEl = categoryEl.querySelector('.ribbon-category__count');
    if (countEl) {
      countEl.textContent = String(items.length);
    }
  }

  // Remove every .ribbon-item matching (itemType, itemId) — handles the
  // one-source-two-items case — then refresh each affected category.
  function removeItemEverywhere(itemType, itemId) {
    var selector = '.ribbon-item[data-item-type="' + itemType +
                   '"][data-item-id="' + itemId + '"]';
    var matches = ribbon.querySelectorAll(selector);
    var touchedCategories = [];
    matches.forEach(function (el) {
      var cat = el.closest('.ribbon-category');
      if (cat && touchedCategories.indexOf(cat) === -1) {
        touchedCategories.push(cat);
      }
      el.remove();
    });
    touchedCategories.forEach(refreshCategory);
  }

  ribbon.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-action]');
    if (!btn) return;
    var action = btn.getAttribute('data-action');
    if (action !== 'dismiss' && action !== 'check') return;

    var itemEl = btn.closest('.ribbon-item');
    if (!itemEl) return;
    var itemType = itemEl.getAttribute('data-item-type');
    var itemId = itemEl.getAttribute('data-item-id');
    if (!itemType || !itemId) return;

    // Optimistic-disable both controls on this item so a double-click
    // can't fire two POSTs; re-enable on failure.
    var controls = itemEl.querySelectorAll('[data-action]');
    controls.forEach(function (c) { c.disabled = true; });

    var url = '/partner/ribbon/' + action + '/' +
              encodeURIComponent(itemType) + '/' + encodeURIComponent(itemId);

    fetch(url, { method: 'POST', credentials: 'same-origin' })
      .then(function (r) {
        // 401 → session expired; send the user to re-auth rather than
        // silently swallow it.
        if (r.status === 401) {
          window.location = '/keypad-login';
          return null;
        }
        return r.json().catch(function () { return null; });
      })
      .then(function (data) {
        if (data && data.ok) {
          // dismiss and check both make the item leave the ribbon.
          removeItemEverywhere(itemType, itemId);
        } else {
          // 403 / 404 / 400 / 5xx — re-enable so the user can retry or
          // move on; surface the reason in the console for debugging.
          controls.forEach(function (c) { c.disabled = false; });
          if (data && data.error) {
            console.warn('ribbon ' + action + ' failed:', data.error);
          } else {
            console.warn('ribbon ' + action + ' failed');
          }
        }
      })
      .catch(function (err) {
        controls.forEach(function (c) { c.disabled = false; });
        console.warn('ribbon ' + action + ' network error:', err);
      });
  });
})();
