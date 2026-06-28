from pathlib import Path


def test_sidebar_close_clears_legacy_body_open_class():
    script = Path("app/static/js/sidebar.js").read_text(encoding="utf-8")

    assert "document.body.classList.add('sidebar-open');" in script
    assert "document.body.classList.remove('sidebar-open');" in script


def test_sidebar_x_uses_shared_close_helper():
    partial = Path("app/templates/partials/sidebar.html").read_text(encoding="utf-8")
    base = Path("app/templates/base_dashboard.html").read_text(encoding="utf-8")

    assert 'onclick="window.closeSidebar && window.closeSidebar();"' in partial
    assert "document.body.classList.toggle('sidebar-open', open);" in base
    assert "document.body.classList.toggle('ck-drawer-open', open);" in base
    assert "host.dataset.open = open ? 'true' : 'false';" in base
