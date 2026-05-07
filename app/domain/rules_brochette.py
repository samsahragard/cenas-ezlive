# brochette rules (tray + individual)
from __future__ import annotations
from app.domain.schemas import NormalizedItem, NormalizedOrder, PrepBreakdown
from app.domain.rules_utils import individual_summary, make_count_line
from app.domain.party_pack_rules import party_sides, tortilla_packets

def rule_brochette(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    headcount = item["qty"]
    packaging = item["choices"]["packaging"]

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
            "summary_line": individual_summary(headcount, "Brochette Shrimp"),
            "flags": item["flags"][:] + ["INDIVIDUAL_PACKAGING_SUMMARY_ONLY"],
        }

    # tray packaging behavior (keep what you were aiming for)
    proteins = []
    packs = 2.0 * headcount
    proteins.append(make_count_line("Shrimp (4-Pack)", total=packs, unit="packs", per_qty=2.0))

    counts = tortilla_packets(headcount, item["choices"]["tortillas"])
    sides = party_sides(headcount, item["choices"]["beans"])

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