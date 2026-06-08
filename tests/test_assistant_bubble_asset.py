from pathlib import Path


def test_dashboard_no_longer_loads_floating_assistant_bubble():
    template = Path("app/templates/base_dashboard.html").read_text(encoding="utf-8")

    assert "assistant_bubble.css" not in template
    assert "assistant_bubble.js" not in template
    assert "ckai-root" not in template


def test_sidebar_keeps_top_left_ai_orb_entry():
    sidebar = Path("app/templates/partials/sidebar.html").read_text(encoding="utf-8")
    css = Path("app/static/css/sidebar.css").read_text(encoding="utf-8")

    assert "ck-sb-ai-orb" in sidebar
    assert "data-ck-ai-orb" in sidebar
    assert "/{{ store_slug }}/today?tab=cena" in sidebar
    assert ".ck-sb-ai-orb" in css
