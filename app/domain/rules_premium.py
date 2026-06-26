from __future__ import annotations

from app.domain.schemas import NormalizedItem, NormalizedOrder, PrepBreakdown
from app.domain.rules_utils import make_count_line, make_weight_line
from app.domain.party_pack_rules import party_sides, tortilla_packets
from app.domain.rules_salads import salad_dressing_lines
from app.domain.containers import container_for_oz


_EXECUTIVE_MIXED_MEAT_OZ = 3.5
_EXECUTIVE_SINGLE_MEAT_OZ = _EXECUTIVE_MIXED_MEAT_OZ * 2
_EXECUTIVE_PARTY_SIDE_OVERRIDES = {
    "Pico De Gallo": 1.0,
    "Guacamole": 1.0,
    "Sour Cream": 1.0,
}
_EXECUTIVE_EXTRA_SIDE_RATES = {
    "Lettuce": 2.5,
    "Avocado Diced": 1.5,
    "Tomatoes Diced": 1.5,
    "Cucumber Diced": 1.5,
    "Grated Cheese": 1.0,
}


def _executive_protein_choice(item: NormalizedItem) -> str:
    for extra in item.get("extras") or []:
        if extra.get("name") == "protein":
            raw = str(extra.get("raw_text") or "").strip().lower()
            if raw in {"chicken", "beef"}:
                return raw
            if raw in {"mix", "mixed", "combo", "beef & chicken", "beef and chicken"}:
                return "mixed"

    source = item.get("source") or {}
    text = " ".join([
        str(source.get("raw_alias") or ""),
        *[str(line) for line in source.get("raw_line_items") or []],
    ]).lower()
    if "beef" in text and "chicken" in text:
        return "mixed"
    if "chicken" in text:
        return "chicken"
    if "beef" in text:
        return "beef"
    return "mixed"


def _executive_party_sides(headcount: int) -> list:
    sides = []
    for line in party_sides(headcount, "charro"):
        override = _EXECUTIVE_PARTY_SIDE_OVERRIDES.get(line["name"])
        if override is None:
            sides.append(line)
            continue
        total_oz = override * headcount
        sides.append(make_weight_line(line["name"], total_oz, override, container_for_oz(total_oz)))
    return sides


def rule_premium(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    if item["item_key"] != "cenas_exec_spread":
        return {
            "item_key": item["item_key"],
            "package_type": item["package_type"],
            "qty": item["qty"],
            "choices": item["choices"],
            "proteins": [],
            "sides": [],
            "sauces": [],
            "extras": [],
            "counts": [],
            "summary_line": f'{item["qty"]}x {item["source"]["raw_alias"]}',
            "flags": item["flags"][:] + ["NO_PREMIUM_RULE_APPLIED"],
        }

    headcount = item["qty"]
    protein_choice = _executive_protein_choice(item)
    if protein_choice == "chicken":
        proteins = [make_weight_line("Chicken", _EXECUTIVE_SINGLE_MEAT_OZ * headcount, _EXECUTIVE_SINGLE_MEAT_OZ, "none")]
    elif protein_choice == "beef":
        proteins = [make_weight_line("Beef", _EXECUTIVE_SINGLE_MEAT_OZ * headcount, _EXECUTIVE_SINGLE_MEAT_OZ, "none")]
    else:
        proteins = [
            make_weight_line("Chicken", _EXECUTIVE_MIXED_MEAT_OZ * headcount, _EXECUTIVE_MIXED_MEAT_OZ, "none"),
            make_weight_line("Beef", _EXECUTIVE_MIXED_MEAT_OZ * headcount, _EXECUTIVE_MIXED_MEAT_OZ, "none"),
        ]

    sides = _executive_party_sides(headcount)
    sides.extend([
        make_weight_line("Queso Blanco", 1.5 * headcount, 1.5),
        make_weight_line("Lettuce", _EXECUTIVE_EXTRA_SIDE_RATES["Lettuce"] * headcount, _EXECUTIVE_EXTRA_SIDE_RATES["Lettuce"]),
        make_weight_line("Avocado Diced", _EXECUTIVE_EXTRA_SIDE_RATES["Avocado Diced"] * headcount, _EXECUTIVE_EXTRA_SIDE_RATES["Avocado Diced"]),
        make_weight_line("Tomatoes Diced", _EXECUTIVE_EXTRA_SIDE_RATES["Tomatoes Diced"] * headcount, _EXECUTIVE_EXTRA_SIDE_RATES["Tomatoes Diced"]),
        make_weight_line("Cucumber Diced", _EXECUTIVE_EXTRA_SIDE_RATES["Cucumber Diced"] * headcount, _EXECUTIVE_EXTRA_SIDE_RATES["Cucumber Diced"]),
        make_weight_line("Grated Cheese", _EXECUTIVE_EXTRA_SIDE_RATES["Grated Cheese"] * headcount, _EXECUTIVE_EXTRA_SIDE_RATES["Grated Cheese"]),
        make_count_line("Churros", 2.0 * headcount, "pieces", 2.0),
    ])

    return {
        "item_key": item["item_key"],
        "package_type": item["package_type"],
        "qty": item["qty"],
        "choices": item["choices"],
        "proteins": proteins,
        "sides": sides,
        "sauces": salad_dressing_lines(headcount, ["Dressing"]),
        "extras": [{"name": "dressing", "raw_text": "Dressing"}],
        "counts": tortilla_packets(headcount, "flour"),
        "summary_line": f'{headcount}ppl - {item["source"]["raw_alias"]}',
        "flags": item["flags"][:],
    }
