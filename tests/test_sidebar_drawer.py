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


def test_role_badge_lives_above_sidebar_logo():
    partial = Path("app/templates/partials/sidebar.html").read_text(encoding="utf-8")
    base = Path("app/templates/base_dashboard.html").read_text(encoding="utf-8")
    css = Path("app/static/css/sidebar.css").read_text(encoding="utf-8")

    assert '<div class="ck-sb-role-badge" aria-label="Current role badge">' in partial
    assert "{% include 'partials/_role_badge.html' %}" in partial
    assert partial.index("ck-sb-role-badge") < partial.index("ck-sb-logo ck-sb-logo-bottom")
    assert "{% include 'partials/_role_badge.html' %}" not in base
    assert ".ck-sb-role-badge .dash-role-banner" in css


def test_tabbed_dashboard_ribbons_do_not_render_role_badges():
    templates = [
        "today_dashboard.html",
        "manager_dashboard.html",
        "operations_dashboard.html",
        "catering_dashboard.html",
        "kitchen_dashboard.html",
        "legal_dashboard.html",
        "vendors_dashboard.html",
    ]

    for name in templates:
        template = Path("app/templates") / name
        text = template.read_text(encoding="utf-8")

        assert "{% include 'partials/_role_badge.html' %}" not in text
        assert "ribbon-role-badge" not in text
        assert "padding-right: clamp(160px, 24vw, 340px);" not in text
        assert "padding-right: 116px;" not in text
