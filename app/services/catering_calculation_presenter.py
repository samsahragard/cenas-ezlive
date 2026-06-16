from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.domain import kitchen_engine
from app.domain.master_sheet_map import MASTER_ROWS
from app.domain.menu_catalog import MenuCatalog, MENU_CATALOG
from app.domain.rules_utils import oz_to_lb


_catalog = MenuCatalog(MENU_CATALOG)
_BUFFER = 3

_INDIVIDUAL_ROW_KEYS = {
    "fajitas_mixed": ("meta.individual.fajitas_mixed", "Beef & Chicken"),
    "fajitas_chicken": ("meta.individual.fajitas_chicken", "Chicken"),
    "fajitas_beef": ("meta.individual.fajitas_beef", "Beef"),
    "veggie_fajitas": ("meta.individual.veggie_fajitas", "Veggie"),
    "brochette_shrimp": ("meta.individual.brochette_shrimp", "Brochette"),
    "cobb_salad": ("meta.individual.cobb_salad", "Cobb Salad"),
    "fajitas_and_salad": ("meta.individual.fajitas_and_salad", "Fajita Salad"),
}

_SIDE_CONTAINER_DEFAULTS = {
    "queso_and_chips": "quart",
    "guac_and_chips": "pint",
    "rice": "quart",
    "refried_beans": "quart",
    "black_beans": "quart",
    "charro_beans": "quart",
    "grated_cheese": "pint",
    "pickled_jalapenos": "pint",
    "fresh_jalapenos": "pint",
    "sour_cream": "pint",
    "pico_de_gallo": "pint",
    "red_sauce": "pint",
    "green_sauce": "pint",
    "fresh_avocado": "pint",
}

_UTENSIL_LABELS = {
    "plates_and_bowls": "Plates",
    "silverware": "Silverware",
    "catering_large_spoons": "Black Large Spoons",
    "catering_small_spoons": "Black Small Spoons",
    "black_tongs": "Black Tongs",
}


def _fmt_num(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _fmt_oz(value: float | int | None) -> str:
    return f"{_fmt_num(float(value or 0))}oz"


def _line_name(raw: str) -> str:
    if raw.startswith("Dressing - "):
        return raw[len("Dressing - "):]
    return raw


def _append_calc(calcs: dict[str, list[str]], key: str, text: str) -> None:
    if text and text not in calcs[key]:
        calcs[key].append(text)


def _weight_calc(qty: int, line: dict[str, Any]) -> str:
    total_oz = float(line.get("total") or 0)
    per_qty = line.get("per_qty")
    total_lb = oz_to_lb(total_oz)
    if per_qty not in (None, ""):
        return f"{qty} x {_fmt_oz(per_qty)} = {_fmt_oz(total_oz)} = {_fmt_num(total_lb)}lb"
    return f"{_fmt_oz(total_oz)} = {_fmt_num(total_lb)}lb"


def _count_calc(qty: int, line: dict[str, Any]) -> str:
    total = line.get("total") or 0
    per_qty = line.get("per_qty")
    unit = line.get("unit") or "count"
    if per_qty not in (None, ""):
        return f"{qty} x {_fmt_num(float(per_qty))} {unit} = {_fmt_num(float(total))} {unit}"
    return f"Counted total = {_fmt_num(float(total))} {unit}"


def _packet_calc(qty: int, packet: dict[str, Any], all_packets: list[dict[str, Any]]) -> str:
    packets = packet.get("packets") or 0
    unit = packet.get("unit") or "packets"
    raw_packets = float(packet.get("raw_packets") or 0)
    if len(all_packets) > 1:
        total_raw = sum(float(p.get("raw_packets") or 0) for p in all_packets)
        split = " / ".join(
            f"{p.get('packets') or 0} {str(p.get('name') or '').replace(' Tortillas', '').lower()}"
            for p in all_packets
        )
        return f"{qty} x 2.5 tortillas ÷ 2 per packet = {_fmt_num(total_raw)} packets; rounded and split to {split}"
    return f"{qty} x 2.5 tortillas ÷ 2 per packet = {_fmt_num(raw_packets)}; rounded to {packets} {unit}"


def _pdf_counts(item: dict[str, Any] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    if not item:
        return out
    for extra in item.get("extras") or []:
        try:
            out[extra.get("name")] = int(extra.get("raw_text"))
        except (TypeError, ValueError):
            pass
    return out


def _tableware_item(order: dict[str, Any], key: str) -> dict[str, Any] | None:
    for item in order.get("normalized_items") or []:
        if item.get("item_key") == key:
            return item
    return None


def _tool_counts(order: dict[str, Any], breakdowns: list[dict[str, Any]]) -> dict[str, int]:
    sides_spoon_keys = {
        i.get("item_key")
        for i in order.get("normalized_items") or []
        if i.get("package_type") == "sides"
        and i.get("item_key") in (kitchen_engine._SIDES_SMALL_SPOON_KEYS | kitchen_engine._SAUCE_KEYS)
    }
    sides_tong_keys = {
        i.get("item_key")
        for i in order.get("normalized_items") or []
        if i.get("package_type") in {"sides", "a_la_carte"}
        and i.get("item_key") in kitchen_engine._SIDES_TONGS_KEYS
    }

    prep_tongs = 0
    prep_large = 0
    prep_small = 0
    seen_tongs: set[str] = set()
    seen_large: set[str] = set()
    seen_small: set[str] = set()

    for b in breakdowns:
        if b.get("package_type") == "desserts" and b.get("item_key") in kitchen_engine._DESSERT_TONGS_KEYS:
            prep_tongs += 1
            seen_tongs.add(str(b.get("item_key")))
        if b.get("package_type") == "enchiladas" and (b.get("choices") or {}).get("packaging") != "individual":
            prep_large += int(b.get("qty") or 0)
        if kitchen_engine._is_individual_meal(b):
            continue
        if b.get("package_type") not in kitchen_engine._TRAY_TYPES:
            continue
        for line in (b.get("proteins") or []) + (b.get("sides") or []) + (b.get("sauces") or []):
            name = line.get("name")
            if name in kitchen_engine._PREP_TONG_COMPONENT_VALUES and name not in seen_tongs:
                prep_tongs += 1
                seen_tongs.add(name)
            if name in kitchen_engine._PREP_LARGE_SPOON_COMPONENT_VALUES and name not in seen_large:
                prep_large += 1
                seen_large.add(name)
            if name in kitchen_engine._PREP_SMALL_SPOON_COMPONENT_VALUES and name not in seen_small:
                prep_small += 1
                seen_small.add(name)
            if name in {"Pico De Gallo", "Tomatoes Diced", "Cucumber Diced", "Black Olives"} and name not in seen_small:
                prep_small += 1
                seen_small.add(name)
            if str(name or "").startswith("Dressing") and "Dressing" not in seen_small:
                prep_small += 1
                seen_small.add("Dressing")

    return {
        "side_spoons": len(sides_spoon_keys),
        "side_tongs": len(sides_tong_keys),
        "prep_tongs": prep_tongs,
        "prep_large": prep_large,
        "prep_small": prep_small,
    }


def _tableware_calcs(order: dict[str, Any], result: dict[str, Any]) -> dict[str, str]:
    breakdowns = result.get("breakdowns") or []
    bulk_qty = kitchen_engine._bulk_meal_qty(order)
    individual_qty = kitchen_engine._individual_meal_qty(order)
    headcount = int(order.get("headcount") or 0)
    tableware_item = _tableware_item(order, "tableware")
    plates_item = _tableware_item(order, "plates_and_bowls")
    pdf = _pdf_counts(tableware_item)
    plate_pdf = _pdf_counts(plates_item)
    tools = _tool_counts(order, breakdowns)

    calcs: dict[str, str] = {}
    if bulk_qty or individual_qty:
        meal_parts = []
        if bulk_qty:
            meal_parts.append(f"{bulk_qty} bulk")
        if individual_qty:
            meal_parts.append(f"{individual_qty} individual")
        calcs["silverware"] = f"{' + '.join(meal_parts)} + {_BUFFER} buffer = {bulk_qty + individual_qty + _BUFFER}"
        calcs["plates_and_bowls"] = (
            f"{bulk_qty} bulk + {_BUFFER} buffer = {bulk_qty + _BUFFER}"
            if bulk_qty
            else "individual packaging uses 0 plates"
        )
    else:
        silverware_pdf = pdf.get("silverware")
        plates_pdf = pdf.get("plates_and_bowls") or plate_pdf.get("plates_and_bowls")
        calcs["silverware"] = (
            f"PDF silverware count = {silverware_pdf}"
            if silverware_pdf
            else f"{headcount} headcount + {_BUFFER} buffer = {headcount + _BUFFER}"
        )
        calcs["plates_and_bowls"] = (
            f"PDF plate count = {plates_pdf}"
            if plates_pdf
            else f"{headcount} headcount + {_BUFFER} buffer = {headcount + _BUFFER}"
        )

    large_pdf = pdf.get("catering_large_spoons") or 0
    small_pdf = pdf.get("catering_small_spoons")
    tongs_pdf = pdf.get("black_tongs")
    small_base = small_pdf if small_pdf is not None else tools["side_spoons"]
    tongs_base = tongs_pdf if tongs_pdf is not None else tools["side_tongs"]
    small_source = "PDF small spoons" if small_pdf is not None else "side/sauce spoon types"
    tongs_source = "PDF tongs" if tongs_pdf is not None else "side/a-la-carte tong types"

    calcs["catering_large_spoons"] = (
        f"PDF large spoons {large_pdf} + tray items needing large spoons {tools['prep_large']} = "
        f"{large_pdf + tools['prep_large']}"
    )
    calcs["catering_small_spoons"] = (
        f"{small_source} {small_base} + tray items needing small spoons {tools['prep_small']} = "
        f"{small_base + tools['prep_small']}"
    )
    calcs["black_tongs"] = (
        f"{tongs_source} {tongs_base} + tray/dessert items needing tongs {tools['prep_tongs']} = "
        f"{tongs_base + tools['prep_tongs']}"
    )
    return calcs


def _build_calculation_map(bundle: dict[str, Any]) -> dict[str, list[str]]:
    order = bundle["normalized_order"]
    result = bundle["kitchen_result"]
    calcs: dict[str, list[str]] = defaultdict(list)

    for breakdown in result.get("breakdowns") or []:
        qty = int(breakdown.get("qty") or 0)
        if "INDIVIDUAL_PACKAGING_SUMMARY_ONLY" in (breakdown.get("flags") or []):
            row = _INDIVIDUAL_ROW_KEYS.get(str(breakdown.get("item_key") or ""))
            if row:
                _append_calc(calcs, row[0], f"{qty} ordered as individual packages")
            else:
                _append_calc(calcs, "meta.individual", f"{qty} ordered as individual packages")

        for line in (breakdown.get("proteins") or []) + (breakdown.get("sides") or []):
            key = f"component.{line.get('name')}"
            if line.get("measure_type") == "weight":
                _append_calc(calcs, key, _weight_calc(qty, line))
            else:
                _append_calc(calcs, key, _count_calc(qty, line))

        for line in breakdown.get("sauces") or []:
            key = f"component.{line.get('name')}"
            if str(line.get("name") or "").startswith("Dressing"):
                name = _line_name(str(line.get("name") or "Dressing"))
                if (breakdown.get("choices") or {}).get("packaging") == "individual":
                    _append_calc(
                        calcs,
                        "item.salad_dressing",
                        f"{qty} x {_fmt_oz(line.get('per_qty'))} {name} portion cups = {qty} cups",
                    )
                else:
                    _append_calc(calcs, "item.salad_dressing", f"{qty} x {_fmt_oz(line.get('per_qty'))} {name} = {_fmt_oz(line.get('total'))}")
            if line.get("measure_type") == "weight":
                _append_calc(calcs, key, _weight_calc(qty, line))
            else:
                _append_calc(calcs, key, _count_calc(qty, line))

        packets = breakdown.get("counts") or []
        for packet in packets:
            _append_calc(calcs, f"component.{packet.get('name')}", _packet_calc(qty, packet, packets))

    for item in order.get("normalized_items") or []:
        item_key = item.get("item_key")
        sheet_meta = _catalog.get_sheet_meta(item_key)
        if not sheet_meta:
            continue
        section = sheet_meta.get("section")
        if section not in {"A_LA_CARTE", "SIDES", "ENCHILADAS", "DRINKS_DESSERTS", "NON_FOOD", "PREMIUM", "SALADS"}:
            continue
        if section == "SALADS" and (item.get("choices") or {}).get("packaging") == "individual":
            continue
        qty = _fmt_num(float(item.get("qty") or 0))
        if section == "SIDES":
            container = item.get("container") or _SIDE_CONTAINER_DEFAULTS.get(str(item_key or ""))
            suffix = f"; unit shown as {container}" if container else ""
            _append_calc(calcs, f"item.{item_key}", f"{qty} ordered{suffix}")
        else:
            _append_calc(calcs, f"item.{item_key}", f"{qty} ordered")

    tableware_calcs = _tableware_calcs(order, result)
    for component, text in tableware_calcs.items():
        _append_calc(calcs, f"item.{component}", text)

    return calcs


def build_catering_calculation_detail(bundle: dict[str, Any], grids: dict[str, Any]) -> dict[str, Any]:
    master_grid = grids["master"]
    master_column = next(
        (col for col in master_grid["columns"] if col["order_id"] != "Total"),
        master_grid["columns"][0],
    )
    values = master_column["values"]
    calc_map = _build_calculation_map(bundle)

    rows: list[dict[str, str]] = []
    for spec in MASTER_ROWS:
        key = spec["key"]
        amount = values.get(key, "")
        if amount in ("", None):
            continue
        if spec["section"] == "Header":
            calculation = "Stored order field"
        else:
            calculation = " | ".join(calc_map.get(key) or ["Direct order value"])
        rows.append({
            "key": key,
            "section": spec["section"],
            "label": spec["label"],
            "amount": str(amount),
            "calculation": calculation,
        })

    item_rows = []
    for item in bundle["normalized_order"].get("normalized_items") or []:
        source = item.get("source") or {}
        item_rows.append({
            "qty": _fmt_num(float(item.get("qty") or 0)),
            "name": source.get("raw_alias") or item.get("item_key") or "",
            "package_type": item.get("package_type") or "",
            "packaging": (item.get("choices") or {}).get("packaging") or "",
        })

    return {
        "rows": rows,
        "items": item_rows,
    }
