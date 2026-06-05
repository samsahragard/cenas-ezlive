from pathlib import Path


def test_assistant_bubble_waits_for_enabled_context_before_showing():
    script = Path("app/static/js/assistant_bubble.js").read_text(encoding="utf-8")

    root_hidden = 'root.setAttribute("hidden", "hidden");'
    root_shown = "root.removeAttribute(\"hidden\");"

    assert root_hidden in script
    assert root_shown in script
    assert script.index(root_hidden) < script.index('fetch("/assistant/context"')
    assert script.index(root_shown) > script.index("!data.enabled")
