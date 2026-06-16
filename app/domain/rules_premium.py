from __future__ import annotations

from app.domain.schemas import NormalizedItem, NormalizedOrder, PrepBreakdown
from app.domain.rules_utils import make_count_line, make_weight_line
from app.domain.party_pack_rules import party_sides, tortilla_packets
from app.domain.rules_salads import salad_dressing_lines


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
    proteins = [
        make_weight_line("Chicken", 2.5 * headcount, 2.5, "none"),
        make_weight_line("Beef", 2.5 * headcount, 2.5, "none"),
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
