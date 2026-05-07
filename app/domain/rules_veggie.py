# veggie rules (tray + individual)
from __future__ import annotations
from app.domain.schemas import NormalizedItem, NormalizedOrder, PrepBreakdown
from app.domain.rules_utils import individual_summary, make_weight_line
from app.domain.party_pack_rules import party_sides, tortilla_packets

def rule_veggie(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    packaging = item["choices"]["packaging"]
    headcount = item["qty"]

    if packaging == "individual":
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
            "summary_line": individual_summary(headcount, "Veggie Fajita"),
            "flags": item["flags"][:] + ["INDIVIDUAL_PACKAGING_SUMMARY_ONLY"],
        }

    # tray packaging (if you want to keep tray veggie as “party sides + tortillas”)
    proteins = []
    veggie_oz = 6.0 * headcount
    proteins.append(make_weight_line("Veggie", veggie_oz, 6.0, "none"))

    sides = party_sides(headcount, item["choices"]["beans"])
    counts = tortilla_packets(headcount, item["choices"]["tortillas"])

    return {
        "item_key": item["item_key"],
        "package_type": item["package_type"],
        "qty": item["qty"],
        "choices": item["choices"],
        "proteins": proteins,
        "sides": sides,
        "sauces": [],
        "extras": [],
        "counts": counts,
        "summary_line": f'{headcount}ppl - {item["source"]["raw_alias"]}',
        "flags": item["flags"][:],
    }