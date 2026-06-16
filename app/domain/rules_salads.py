# Calculations for salad packages including dressing choices
from __future__ import annotations
from app.domain.schemas import NormalizedItem, NormalizedOrder, PrepBreakdown
from app.domain.rules_utils import make_weight_line, individual_summary


def _dressing_note(dressings: list[str]) -> str:
    return ", ".join(dressings) if dressings else "Not Specified"


def salad_dressing_lines(headcount: int, dressings: list[str]) -> list[dict]:
    if len(dressings) >= 2:
        return [
            make_weight_line(f"Dressing - {name}", 1.5 * headcount, 1.5)
            for name in dressings
        ]

    name = dressings[0] if dressings else "Dressing"
    return [make_weight_line(f"Dressing - {name}", 3.0 * headcount, 3.0)]

def rule_cobb_salad(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    headcount = item["qty"]
    dressings = [e["raw_text"] for e in item["extras"] if e["name"] == "dressing"]
    dressing_note = _dressing_note(dressings)

    if item["choices"]["packaging"] == "individual":
        return {
            "item_key": item["item_key"],
            "package_type": item["package_type"],
            "qty": item["qty"],
            "choices": item["choices"],
            "proteins": [],
            "sides": [],
            "sauces": salad_dressing_lines(headcount, dressings),
            "extras": [],
            "counts": [],
            "summary_line": f"{individual_summary(item['qty'], 'Cobb Salad')} | Dressing: {dressing_note}",
            "flags": item["flags"][:] + ["INDIVIDUAL_PACKAGING_SUMMARY_ONLY"],
        }

    protein_choice = next((e["raw_text"] for e in item["extras"] if e["name"] == "protein"), "combo")

    proteins = []
    if protein_choice in ("chicken_diced", "combo"):
        proteins.append(make_weight_line("Chicken Diced", 2.0 * headcount, 2.0, "none"))
    if protein_choice in ("beef_diced", "combo"):
        proteins.append(make_weight_line("Beef Diced", 2.0 * headcount, 2.0, "none"))

    sides = [
        make_weight_line("Lettuce", 4.0 * headcount, 4.0),
        make_weight_line("Avocado Diced", 2.0 * headcount, 2.0),
        make_weight_line("Tomatoes Diced", 2.0 * headcount, 2.0),
        make_weight_line("Cucumber Diced", 2.0 * headcount, 2.0),
        make_weight_line("Grated Cheese", 2.0 * headcount, 2.0),
        make_weight_line("Bacon", 1.0 * headcount, 1.0),
        make_weight_line("Egg", 2.0 * headcount, 2.0),
        make_weight_line("Black Olives", 1.0 * headcount, 1.0),
    ]
    sauces = salad_dressing_lines(headcount, dressings)

    return {
        "item_key": item["item_key"],
        "package_type": item["package_type"],
        "qty": item["qty"],
        "choices": item["choices"],
        "proteins": proteins,
        "sides": sides,
        "sauces": sauces,
        "extras": [{"name": "dressing", "raw_text": dressing_note}],
        "counts": [],
        "summary_line": f'{headcount}ppl - Cobb Salad | Dressing: {dressing_note}',
        "flags": item["flags"][:],
    }

def rule_fajitas_and_salad(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    headcount = item["qty"]
    dressings = [e["raw_text"] for e in item["extras"] if e["name"] == "dressing"]
    dressing_note = _dressing_note(dressings)

    if item["choices"]["packaging"] == "individual":
        return {
            "item_key": item["item_key"],
            "package_type": item["package_type"],
            "qty": item["qty"],
            "choices": item["choices"],
            "proteins": [],
            "sides": [],
            "sauces": salad_dressing_lines(headcount, dressings),
            "extras": [],
            "counts": [],
            "summary_line": f"{individual_summary(item['qty'], 'Fajita Salad')} | Dressing: {dressing_note}",
            "flags": item["flags"][:] + ["INDIVIDUAL_PACKAGING_SUMMARY_ONLY"],
        }
    
    protein_choice = next((e["raw_text"] for e in item["extras"] if e["name"] == "protein"), "mix")

    proteins = []
    if protein_choice in ("chicken", "mix"):
        proteins.append(make_weight_line("Chicken", 2.5 * headcount, 2.5, "none"))
    if protein_choice in ("beef", "mix"):
        proteins.append(make_weight_line("Beef", 2.5 * headcount, 2.5, "none"))

    sides = [
        make_weight_line("Lettuce", 4.0 * headcount, 4.0),
        make_weight_line("Avocado Diced", 2.0 *headcount, 2.0),
        make_weight_line("Tomatoes Diced", 2.0 * headcount, 2.0),
        make_weight_line("Cucumber Diced", 2.0 * headcount, 2.0),
        make_weight_line("Grated Cheese", 2.0 * headcount, 2.0),
    ]

    sauces = salad_dressing_lines(headcount, dressings)

    return {
        "item_key": item["item_key"],
        "package_type": item["package_type"],
        "qty": item["qty"],
        "choices": item["choices"],
        "proteins": proteins,
        "sides": sides,
        "sauces": sauces,
        "extras": [{"name": "dressing", "raw_text": dressing_note}],
        "counts": [],
        "summary_line": f'{headcount}ppl - Fajita & Salad | Dressing: {dressing_note}',
        "flags": item["flags"][:],
    }

def rule_salads(item: NormalizedItem, order: NormalizedOrder) -> PrepBreakdown:
    if item["item_key"] == "cobb_salad":
        return rule_cobb_salad(item, order)
    if item["item_key"] == "fajitas_and_salad":
        return rule_fajitas_and_salad(item, order)
    return {
        "item_key": item["item_key"],
        "package_type": item["package_type"],
        "qty": item["qty"],
        "choices": item["choices"],
        "proteins": [],
        "sides": [],
        "sauces": [],
        "extras": item["extras"][:],
        "counts": [],
        "summary_line": f'{item["qty"]}x {item["source"]["raw_alias"]}',
        "flags": item["flags"][:],
    }
