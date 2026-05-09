// Mobile sidebar drawer
function toggleSidebar() {
  document.body.classList.toggle('sidebar-open');
}

function closeSidebar() {
  document.body.classList.remove('sidebar-open');
}

// Nested nav groups (Vendors, Ezcater, Schedule, Performance, Sales, Labor)
// Click the parent to toggle its children. Click on a child link auto-bubbles
// to the parent's normal anchor behavior since the child is just an <a>.
function toggleNavGroup(btn) {
  const group = btn.closest('.nav-group, .nav-subgroup');
  if (group) group.classList.toggle('expanded');
}

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') closeSidebar();
});
