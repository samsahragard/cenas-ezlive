// Mobile sidebar drawer
function toggleSidebar() {
  document.body.classList.toggle('sidebar-open');
}

function closeSidebar() {
  document.body.classList.remove('sidebar-open');
}

// Click-to-toggle nested nav groups (plain inline accordion). Only used
// for subgroups now — top-level groups are <a> links that navigate to a
// landing page, and the server-side _open flags expand the right group on
// the destination page so click → navigate also "opens" it visually.
function toggleNavGroup(btn) {
  const group = btn.closest('.nav-group, .nav-subgroup');
  if (group) group.classList.toggle('expanded');
}

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') {
    closeSidebar();
    document.querySelectorAll('.nav-group.expanded, .nav-subgroup.expanded')
      .forEach((g) => g.classList.remove('expanded'));
  }
});
