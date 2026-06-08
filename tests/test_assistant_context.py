from pathlib import Path

from app.services.assistant_context import (
    CONTEXT_FILES,
    build_assistant_context,
    load_assistant_context,
)


def test_loader_reads_default_context_files():
    context = load_assistant_context()

    assert context.startswith("# Cenas Kitchen Business Context")
    assert "# Company" in context
    assert "# People" in context
    assert "# Stores" in context
    assert "# Glossary" in context
    assert "Cenas Kitchen, LLC" in context
    assert "DOS MAS" in context
    assert "UNO MAS" in context
    assert "TODO (Sam to confirm)" in context


def test_loader_uses_expected_context_file_order():
    context = load_assistant_context()

    positions = [
        context.index(f"\n\n# {Path(filename).stem.title()}\n")
        for filename in CONTEXT_FILES
    ]
    assert positions == sorted(positions)


def test_build_alias_matches_loader():
    assert build_assistant_context() == load_assistant_context()


def test_loader_degrades_gracefully_when_file_is_missing(tmp_path):
    (tmp_path / "COMPANY.md").write_text("# Company\n\nKnown fact.", encoding="utf-8")

    context = load_assistant_context(
        context_dir=tmp_path,
        filenames=("COMPANY.md", "PEOPLE.md"),
    )

    assert "Known fact." in context
    assert "TODO (Sam to confirm): context file `PEOPLE.md` is missing." in context
    assert context.endswith("\n")
