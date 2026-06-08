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
    assert 'href="/assistant"' in sidebar
    assert "/{{ store_slug }}/today?tab=cena" not in sidebar
    assert ".ck-sb-ai-orb" in css


def test_bottom_nav_ai_orb_links_to_standalone_assistant():
    bottom_nav = Path("app/templates/partials/_bottom_nav.html").read_text(encoding="utf-8")

    assert 'href="/assistant"' in bottom_nav
    assert "/{{ store_slug }}/today?tab=cena" not in bottom_nav


def test_today_dashboard_no_longer_embeds_ai_tabs():
    routes = Path("app/web/store_routes.py").read_text(encoding="utf-8")
    template = Path("app/templates/today_dashboard.html").read_text(encoding="utf-8")

    assert '("cena",          "Cenas AI")' not in routes
    assert '("cena-dev",      "Cena + Dev")' not in routes
    assert 'if tab_key == "cena"' not in routes
    assert 'if tab_key == "cena-dev"' not in routes
    assert "'cena':" not in template
    assert "'cena-dev':" not in template


def test_ai_orb_only_treats_standalone_assistant_as_ai_surface():
    script = Path("app/static/js/ai_orb.js").read_text(encoding="utf-8")

    assert 'path.indexOf("/assistant") !== -1' in script
    assert 'params.get("tab") === "cena"' not in script


def test_assistant_page_is_clean_chat_surface():
    template = Path("app/templates/assistant_page.html").read_text(encoding="utf-8")

    assert "{% block page_title %}C.E.N.A{% endblock %}" in template
    assert "aiassist-tools" not in template
    assert "active tools" not in template
    assert "Role catalog loaded" not in template
    assert "assistant.general_help" not in template
    assert "/assistant/review-items" in template
    assert "Saved for Sam" in template
