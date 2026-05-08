function toggleSidebar() {
  document.body.classList.toggle('sidebar-open');
}

function closeSidebar() {
  document.body.classList.remove('sidebar-open');
}

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') closeSidebar();
});
