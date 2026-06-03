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
    replace: bool = False,
    skip_if_populated: bool = False,
) -> tuple[int, int, int]:
    """Insert recipes from the JSON fixture.

    Modes:
      - replace=True: wipe ALL existing recipes, then insert every fixture
        row (codes need not be unique; used by the gated replace endpoint).
      - skip_if_populated=True: no-op if the table already holds any recipe
        (used at boot so a populated prod table is never auto-mutated; a
        fresh/empty install still gets seeded).
      - default (both False): additive — insert only codes not already present.
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
        if skip_if_populated and not replace:
            if db.query(Recipe.id).first() is not None:
                logger.info(
                    "recipes seed: table populated -> skip_if_populated no-op")
                return (0, 0, 0)

        if replace:
            deleted = db.query(Recipe).delete()
            db.flush()
            logger.info(
                "recipes seed REPLACE: deleted %d existing rows", deleted)
            existing_codes = None  # insert every fixture row
        else:
            existing_codes = {
                c[0] for c in db.query(Recipe.code).filter(Recipe.code.isnot(None)).all()
            }

        for r in recipes:
            code = (r.get("code") or "").strip()
            if existing_codes is not None:
                if not code:
                    logger.warning("seed: skipping recipe with no code: %r",
                                   r.get("name"))
                    errored += 1
                    continue
                if code in existing_codes:
                    skipped += 1
                    continue
            # yield (EN/ES) lives in batch_sizes_json; fall back to a legacy
            # batch_sizes list if the fixture predates the yield fields.
            _ye, _ys = r.get("yield_en"), r.get("yield_es")
            if _ye or _ys:
                _batch_blob = json.dumps({"yield_en": _ye, "yield_es": _ys})
            else:
                _batch_blob = json.dumps(r.get("batch_sizes") or [])
            try:
                row = Recipe(
                    code=(code or None),
                    category=(r.get("category") or "hot").strip().lower()[:40],
                    name=(r.get("name") or "Untitled").strip()[:200],
                    prep_time=(r.get("prep_time_en") or r.get("prep_time") or None),
                    prep_time_es=(r.get("prep_time_es") or None),
                    shelf_life=(r.get("shelf_life_en") or r.get("shelf_life") or None),
                    shelf_life_es=(r.get("shelf_life_es") or None),
                    spanish_instructions=r.get("spanish_instructions") or None,
                    english_instructions=r.get("english_instructions") or None,
                    ingredients_json=json.dumps(r.get("ingredients") or []),
                    batch_sizes_json=_batch_blob,
                    notes=(r.get("notes_en") or r.get("notes") or None),
                )
                db.add(row)
                created += 1
                if existing_codes is not None:
                    existing_codes.add(code)
            except Exception:
                logger.exception("seed: insert failed for code=%s", code)
                errored += 1
        db.commit()
    finally:
        db.close()

    logger.info(
        "recipes seed: created=%d skipped=%d errored=%d (replace=%s)",
        created, skipped, errored, replace)
    return created, skipped, errored
