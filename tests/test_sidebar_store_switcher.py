from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_multi_store_sidebar_renders_store_switcher():
    sidebar = (ROOT / "app/templates/partials/sidebar.html").read_text(encoding="utf-8")
    css = (ROOT / "app/static/css/sidebar.css").read_text(encoding="utf-8")

    assert "_accessible|length > 1" in sidebar
    assert "ck-sb-store-switch" in sidebar
    assert "ck-sb-store-link" in sidebar
    assert 'href="/{{ _slug }}{{ _store_suffix }}{{ _qs }}"' in sidebar
    assert ".ck-sb-store-link.active" in css
