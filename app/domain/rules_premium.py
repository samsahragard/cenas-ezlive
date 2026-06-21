from __future__ import annotations

from app.domain.schemas import NormalizedItem, NormalizedOrder, PrepBreakdown
from app.domain.rules_utils import make_count_line, make_weight_line
from app.domain.party_pack_rules import party_sides, tortilla_packets
from app.domain.rules_salads import salad_dressing_lines


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
        proteins = [make_weight_line("Chicken", 6.0 * headcount, 6.0, "none")]
    elif protein_choice == "beef":
        proteins = [make_weight_line("Beef", 6.0 * headcount, 6.0, "none")]
    else:
        proteins = [
            make_weight_line("Chicken", 3.0 * headcount, 3.0, "none"),
            make_weight_line("Beef", 3.0 * headcount, 3.0, "none"),
        ]

    sides = party_sides(headcount, "charro")
    sides.extend([
        make_weight_line("Queso Blanco", 1.5 * headcount, 1.5),
        make_weight_line("Lettuce", 4.0 * headcount, 4.0),
        make_weight_line("Avocado Diced", 2.0 * headcount, 2.0),
        make_weight_line("Tomatoes Diced", 2.0 * headcount, 2.0),
        make_weight_line("Cucumber Diced", 2.0 * headcount, 2.0),
        make_weight_line("Grated Cheese", 2.0 * headcount, 2.0),
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
