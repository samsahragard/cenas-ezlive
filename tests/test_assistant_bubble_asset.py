from pathlib import Path


def test_assistant_bubble_waits_for_enabled_context_before_showing():
    script = Path("app/static/js/assistant_bubble.js").read_text(encoding="utf-8")

    root_hidden = 'root.setAttribute("hidden", "hidden");'
    root_shown = "root.removeAttribute(\"hidden\");"

    assert root_hidden in script
    assert root_shown in script
    assert "window.self !== window.top" in script
    assert "function dedupeRoots()" in script
    assert script.index(root_hidden) < script.index('fetch("/assistant/context"')
    assert script.index(root_shown) > script.index("!data.enabled")


def test_assistant_bubble_script_is_versioned_for_mobile_cache():
    template = Path("app/templates/base_dashboard.html").read_text(encoding="utf-8")

    assert "assistant_bubble.js') }}?v={{ config.get('RENDER_GIT_COMMIT', 'local')[:7] }}" in template


def test_assistant_bubble_sends_previous_question_for_followups():
    script = Path("app/static/js/assistant_bubble.js").read_text(encoding="utf-8")

    assert 'var lastUserQuestion = "";' in script
    assert "var previousQuestion = lastUserQuestion;" in script
    assert "lastUserQuestion = question;" in script
    assert "previous_question: previousQuestion" in script


def test_assistant_bubble_does_not_render_on_full_assistant_page():
    script = Path("app/static/js/assistant_bubble.js").read_text(encoding="utf-8")

    assert "function isFullAssistantPage()" in script
    assert 'window.location.pathname === "/assistant"' in script
    assert 'params.get("tab") === "cena"' in script
    assert "if (isFullAssistantPage()) return;" in script
