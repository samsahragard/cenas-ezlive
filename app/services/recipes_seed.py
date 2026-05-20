"""Recipes seed — populate the recipes table from a JSON fixture.

ck build-order #3 (2026-05-19, Sam dev chat #6:53 + #6:56). Idempotent
by code: skips recipes whose code already exists. Reads from
data/recipes/recipes_seed_data.json (committed alongside the seed
script so Render has no parse dependency — pdfplumber not needed).

Callable from anywhere. Returns a count of (created, skipped, errored).
Safe to call multiple times — only inserts missing codes.

Usage:
    from app.services.recipes_seed import seed_recipes_from_json
    created, skipped, errored = seed_recipes_from_json()

Or via the gateway-gated endpoint /sam/cena/run-recipes-seed (added in
store_routes.py for a one-time fire from cena's box).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# Repo-relative path to the JSON fixture. Resolved against the same
# project-root walk as sam_chat's _PROJECT_ROOT pattern.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_FIXTURE_PATH = _PROJECT_ROOT / "data" / "recipes" / "recipes_seed_data.json"


def _load_fixture(path: Path | None = None) -> list[dict]:
    """Load + validate the JSON fixture. Returns the recipes list."""
    p = path or _FIXTURE_PATH
    if not p.exists():
        raise FileNotFoundError(f"recipes seed fixture not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    recipes = data.get("recipes")
    if not isinstance(recipes, list):
        raise ValueError(
            f"recipes seed fixture missing 'recipes' list: {p}")
    return recipes


def seed_recipes_from_json(
    fixture_path: Path | None = None,
) -> tuple[int, int, int]:
    """Insert any recipes whose code is not already in the table.

    Returns (created, skipped, errored).
    """
    from app.db import get_db
    from app.models import Recipe

    recipes = _load_fixture(fixture_path)

    db = next(get_db())
    created = 0
    skipped = 0
    errored = 0
    try:
        existing_codes = {
            c[0] for c in db.query(Recipe.code).filter(Recipe.code.isnot(None)).all()
        }
        for r in recipes:
            code = (r.get("code") or "").strip()
            if not code:
                logger.warning("seed: skipping recipe with no code: %r",
                               r.get("name"))
                errored += 1
                continue
            if code in existing_codes:
                skipped += 1
                continue
            try:
                row = Recipe(
                    code=code,
                    category=(r.get("category") or "hot").strip()[:40],
                    name=(r.get("name") or "Untitled").strip()[:200],
                    prep_time=(r.get("prep_time") or None),
                    shelf_life=(r.get("shelf_life") or None),
                    spanish_instructions=r.get("spanish_instructions") or None,
                    english_instructions=r.get("english_instructions") or None,
                    ingredients_json=json.dumps(r.get("ingredients") or []),
                    batch_sizes_json=json.dumps(r.get("batch_sizes") or []),
                    notes=r.get("notes") or None,
                )
                db.add(row)
                created += 1
                existing_codes.add(code)
            except Exception:
                logger.exception("seed: insert failed for code=%s", code)
                errored += 1
        db.commit()
    finally:
        db.close()

    logger.info(
        "recipes seed: created=%d skipped=%d errored=%d",
        created, skipped, errored)
    return created, skipped, errored
