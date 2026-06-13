"""Import-safe Cenas Kitchen business context loader for the assistant.

Vendored from app/services/assistant_context.py. The only change for the cloud
is CONTEXT_DIR: it points at the package-local ``business_context/`` folder
(the four COMPANY/PEOPLE/STORES/GLOSSARY markdown files vendored alongside this
module). Missing files fall back to a harmless TODO stub, so this is always
import-safe.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable


CONTEXT_DIR = Path(__file__).resolve().parent / "business_context"
CONTEXT_FILES: tuple[str, ...] = (
    "COMPANY.md",
    "PEOPLE.md",
    "STORES.md",
    "GLOSSARY.md",
)


def _context_path(context_dir: str | Path | None) -> Path:
    return Path(context_dir) if context_dir is not None else CONTEXT_DIR


def _title_for(filename: str) -> str:
    return Path(filename).stem.replace("_", " ").title()


def _read_one(base_dir: Path, filename: str) -> str:
    path = base_dir / filename
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return (
            f"# {_title_for(filename)}\n\n"
            f"TODO (Sam to confirm): context file `{filename}` is missing."
        )
    except OSError as exc:
        return (
            f"# {_title_for(filename)}\n\n"
            f"TODO (Sam to confirm): context file `{filename}` could not be read "
            f"({exc.__class__.__name__})."
        )


def load_assistant_context(
    *,
    context_dir: str | Path | None = None,
    filenames: Iterable[str] = CONTEXT_FILES,
) -> str:
    """Return one composed context block ready to drop into the LLM prompt."""

    base_dir = _context_path(context_dir)
    parts = [
        "# Cenas Kitchen Business Context",
        (
            "Use this read-only background to interpret Cenas-specific terms. "
            "Do not treat TODO items as confirmed facts. Do not expose secrets, "
            "raw IDs, passcodes, or private contact details."
        ),
    ]
    parts.extend(_read_one(base_dir, filename) for filename in filenames)
    return "\n\n".join(part for part in parts if part.strip()).strip() + "\n"


def build_assistant_context(
    *,
    context_dir: str | Path | None = None,
    filenames: Iterable[str] = CONTEXT_FILES,
) -> str:
    """Compatibility alias for prompt-building callers."""

    return load_assistant_context(context_dir=context_dir, filenames=filenames)


__all__ = [
    "CONTEXT_DIR",
    "CONTEXT_FILES",
    "build_assistant_context",
    "load_assistant_context",
]
