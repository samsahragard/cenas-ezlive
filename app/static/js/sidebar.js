// Mobile sidebar drawer
function toggleSidebar() {
  document.body.classList.toggle('sidebar-open');
}

function closeSidebar() {
  document.body.classList.remove('sidebar-open');
}

// Position the right-flyout panel for a top-level .nav-group. Called on
// click (and on scroll/resize while the flyout is open). Reads the toggle
// button's rect and sets the panel's fixed-position top/left. Clamps so a
// tall panel doesn't run off the bottom of the viewport.
function positionNavFlyout(group) {
  const toggle = group.querySelector(':scope > .nav-group-toggle');
  const flyout = group.querySelector(':scope > .nav-group-children');
  if (!toggle || !flyout) return;
  const rect = toggle.getBoundingClientRect();
  const panelHeight = flyout.offsetHeight || 200;
  let top = rect.top;
  const margin = 8;
  if (top + panelHeight > window.innerHeight - margin) {
    top = Math.max(margin, window.innerHeight - panelHeight - margin);
  }
  flyout.style.top = top + 'px';
  flyout.style.left = (rect.right + 6) + 'px';
}

// Click-to-toggle nested nav groups. The top-level group flies out to the
// right; subgroups expand inline within the flyout panel.
function toggleNavGroup(btn) {
  const group = btn.closest('.nav-group, .nav-subgroup');
  if (!group) return;
  const isTopLevel = group.classList.contains('nav-group');
  const willExpand = !group.classList.contains('expanded');

  // Only one top-level flyout open at a time — close any other open group.
  if (isTopLevel && willExpand) {
    document.querySelectorAll('.nav-group.expanded').forEach((g) => {
      if (g !== group) g.classList.remove('expanded');
    });
  }

  group.classList.toggle('expanded');
  if (isTopLevel && willExpand) {
    // Position after layout settles so offsetHeight is real.
    requestAnimationFrame(() => positionNavFlyout(group));
  }
}

// Close any open flyout when the user clicks somewhere outside it.
document.addEventListener('click', function (e) {
  if (e.target.closest('.nav-group')) return;
  document.querySelectorAll('.nav-group.expanded').forEach((g) => {
    g.classList.remove('expanded');
  });
});

// Reposition any open flyout on viewport scroll/resize so it stays glued
// to its toggle button. Also covers the new hover-open path: the CSS
// `.nav-group:hover > .nav-group-children` shows the panel, but it still
// needs JS to set its top/left since position: fixed has no anchor.
function repositionOpenFlyouts() {
  document.querySelectorAll('.nav-group').forEach((g) => {
    const flyout = g.querySelector(':scope > .nav-group-children');
    if (flyout && getComputedStyle(flyout).display !== 'none') {
      positionNavFlyout(g);
    }
  });
}
window.addEventListener('scroll', repositionOpenFlyouts, true);
window.addEventListener('resize', repositionOpenFlyouts);

// Hover-to-open path. Two pieces:
//   (a) Pre-position EVERY flyout at page load so when the CSS :hover rule
//       fires display:block, the panel is already at the right spot
//       instead of flashing at viewport (0,0) where position:fixed defaults.
//   (b) Re-position on mouseenter so the spot stays correct after viewport
//       scroll/resize.
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('.nav-group').forEach((g) => {
    positionNavFlyout(g);
    g.addEventListener('mouseenter', () => positionNavFlyout(g));
  });
});

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') {
    closeSidebar();
    document.querySelectorAll('.nav-group.expanded').forEach((g) => g.classList.remove('expanded'));
  }
});
