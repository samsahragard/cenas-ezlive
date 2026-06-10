# fajitas rules (tray + individual)
from __future__ import annotations

from app.domain.schemas import NormalizedItem, NormalizedOrder, PrepBreakdown
from app.domain.rules_utils import make_weight_line, individual_summary
from app.domain.party_pack_rules import party_sides, tortilla_packets

def rule_fajitas(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    packaging = item["choices"]["packaging"]
    headcount = item["qty"]

    if packaging == "individual":
        label = {
            "fajitas_mixed": "Mixed",
            "fajitas_chicken": "Chicken",
            "fajitas_beef": "Beef",
        }.get(item["item_key"], "Fajitas")

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
            "summary_line": individual_summary(headcount, label),
            "flags": item["flags"][:] + ["INDIVIDUAL_PACKAGING_SUMMARY_ONLY"],
        }

    proteins = []
    if item["item_key"] == "fajitas_mixed":
        chicken_oz = 2.5 * headcount
        beef_oz = 2.5 * headcount
        proteins.append(make_weight_line("Chicken", chicken_oz, 2.5, "none"))
        proteins.append(make_weight_line("Beef", beef_oz, 2.5, "none"))
    elif item["item_key"] == "fajitas_chicken":
        chicken_oz = 5.0 * headcount
        proteins.append(make_weight_line("Chicken", chicken_oz, 5.0, "none"))
    elif item["item_key"] == "fajitas_beef":
        beef_oz = 5.0 * headcount
        proteins.append(make_weight_line("Beef", beef_oz, 5.0, "none"))
    else:
        # fallback
        proteins.append(make_weight_line("Fajita Protein", 0, None, "none"))

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


