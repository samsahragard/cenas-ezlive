from pathlib import Path

from flask import Flask, render_template


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_employee_console_css_exposes_required_system_classes():
    css = _read("app/static/css/employee_console.css")

    for token in (
        "--cc-backdrop: #05060a",
        "--cc-ink: #0a0c12",
        "--cc-chit: #141925",
        "--cc-heat: #ff6a3d",
        "--cc-up: #67d6a7",
        "@media (prefers-reduced-motion: reduce)",
    ):
        assert token in css

    for cls in (
        ".cc-hero",
        ".cc-eyebrow",
        ".cc-seg",
        ".cc-card",
        ".cc-delta",
        ".cc-chit",
        ".cc-stop",
        ".cc-toggle-row",
        ".cc-empty",
        ".cc-topbar",
        ".cc-bottom-tabs",
        ".cc-item-line",
        ".cc-profile-card",
    ):
        assert cls in css


def test_employee_nav_has_exactly_five_console_tabs():
    nav = _read("app/templates/partials/_employee_nav.html")

    expected = {
        "Today": "/employee/dashboard",
        "Tables": "/employee/tables",
        "Shifts": "/employee/my-schedule",
        "Inbox": "/employee/messages",
        "You": "/employee/my-profile",
    }
    for label, route in expected.items():
        assert f"'{label}'" in nav
        assert f"'{route}'" in nav
    assert nav.count("('/employee/") == 0
    assert nav.count("'/employee/") == 5
    assert "Time Off" not in nav
    assert "News" not in nav
    assert "aria-current=\"page\"" in nav
    assert "aria-label=\"Employee tabs\"" in nav


def test_employee_console_base_renders_five_tabs():
    app = Flask(__name__, template_folder=str(ROOT / "app/templates"), static_folder=str(ROOT / "app/static"))

    with app.test_request_context("/employee/tables"):
        html = render_template("employee_console_base.html", active_tab="tables", sync_label="synced")

    assert "employee_console.css" in html
    assert html.count('class="cc-tab') == 5
    assert 'href="/employee/tables"' in html
    assert 'aria-current="page"' in html
    assert 'aria-label="Sync status"' in html
    # Topbar logout reachable from every tab.
    assert 'id="cc-logout"' in html


def test_employee_surface_map_lists_real_paths_for_all_five_tabs():
    doc = _read("emp_surface_map.md")
    route_template_pairs = [
        ("/employee/dashboard", "app/templates/employee_dashboard.html"),
        ("/employee/tables", "app/templates/employee_tables.html"),
        ("/employee/my-schedule", "app/templates/employee_schedule.html"),
        ("/employee/messages", "app/templates/employee_messages.html"),
        ("/employee/my-profile", "app/templates/employee_my_profile.html"),
    ]

    for route, template in route_template_pairs:
        assert route in doc
        assert template in doc
        assert (ROOT / template).exists()
