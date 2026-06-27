from pathlib import Path


def test_sidebar_close_clears_legacy_body_open_class():
    script = Path("app/static/js/sidebar.js").read_text(encoding="utf-8")

    assert "document.body.classList.add('sidebar-open');" in script
    assert "document.body.classList.remove('sidebar-open');" in script
