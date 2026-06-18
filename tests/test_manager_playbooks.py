import inspect
from pathlib import Path

from app.web import assistant_routes as ar


def test_manager_playbooks_are_searchable(monkeypatch):
    monkeypatch.setattr(ar, "workspace_path", Path.cwd().resolve())

    expected = {
        "distilled_leadership_rules.md": "smart systems fail",
        "setting_the_table_hospitality.md": "enlightened hospitality",
        "unreasonable_hospitality_playbook.md": "will guidara",
    }
    for filename, query in expected.items():
        path = Path("docs") / "manager_playbooks" / filename
        assert path.exists()

        results = ar.search_manager_playbooks_tool(query)

        assert any(filename in result["file"] for result in results)


def test_assistant_chat_registers_manager_playbook_tool():
    source = inspect.getsource(ar.api_assistant_chat)

    assert "search_manager_playbooks_tool" in source
    assert "Access denied: search_manager_playbooks_tool is restricted to partners and managers." in source
    assert "distilled_leadership_rules.md" in source
    assert "setting_the_table_hospitality.md" in source
    assert "unreasonable_hospitality_playbook.md" in source
    assert "will guidara" in source.lower()
