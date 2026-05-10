"""ezCater per-order revenue calculation.

Every OrderItem.raw_alias the ingest pipeline writes follows the pattern
``"Item Name @ $XX.XX"`` — the unit price the customer was quoted on
ezCater at order time. We can't ask the Partner API for the order total
(catererCart.totals.catererTotalDue returns 403 for our token), so this
module rebuilds it from the per-line unit prices we already have.

Falls back to the scraped storefront menu (``data/ezcater/menu_prices.json``)
when an item line is missing the ``@ $XX.XX`` suffix.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Match the price token at the END of a raw_alias string:
#   "Beef & Chicken Fajita Party Package @ $21.49"  -> 21.49
#   "Tableware"                                      -> no match
_PRICE_RE = re.compile(r"@\s*\$([\d,]+(?:\.\d+)?)")

_MENU_FILE = Path(__file__).resolve().parents[2] / "data" / "ezcater" / "menu_prices.json"


def _load_menu_lookup() -> dict[str, float]:
    """Build a name → price map from the scraped storefront menu.
    Names are normalized (lowercased, whitespace collapsed) so casual
    differences in spelling between the API and the storefront still match."""
    if not _MENU_FILE.exists():
        return {}
    try:
        data = json.loads(_MENU_FILE.read_text(encoding="utf-8"))
        return {_norm(it["name"]): float(it["price"]) for it in (data.get("items") or [])
                if it.get("name") and it.get("price") is not None}
    except Exception:
        logger.exception("failed to load ezCater menu lookup")
        return {}


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


_MENU_LOOKUP: dict[str, float] | None = None


def _menu_lookup() -> dict[str, float]:
    """Lazy + cached load of the storefront menu."""
    global _MENU_LOOKUP
    if _MENU_LOOKUP is None:
        _MENU_LOOKUP = _load_menu_lookup()
    return _MENU_LOOKUP


def parse_unit_price(raw_alias: str) -> float | None:
    """Return the ``$XX.XX`` price embedded in raw_alias, or None if absent."""
    if not raw_alias:
        return None
    m = _PRICE_RE.search(raw_alias)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _item_name_only(raw_alias: str) -> str:
    """Strip the ``@ $XX.XX`` suffix off a raw_alias to get just the name."""
    if not raw_alias:
        return ""
    m = _PRICE_RE.search(raw_alias)
    if not m:
        return raw_alias.strip()
    return raw_alias[: m.start()].strip()


def compute_order_total(items: Iterable) -> float:
    """Sum qty × unit_price across the given OrderItem-like rows.

    Each item is expected to have ``.raw_alias`` (str) and ``.qty`` (int).
    Lines without a parseable unit price are skipped (Tableware, free addons).
    Falls back to the scraped storefront menu when raw_alias has no $ token
    but the name matches a known menu item.
    """
    total = 0.0
    menu = _menu_lookup()
    for it in items:
        qty = int(getattr(it, "qty", 0) or 0)
        if qty <= 0:
            continue
        unit = parse_unit_price(getattr(it, "raw_alias", "") or "")
        if unit is None:
            unit = menu.get(_norm(_item_name_only(getattr(it, "raw_alias", "") or "")))
        if unit is None:
            continue
        total += unit * qty
    return round(total, 2)
