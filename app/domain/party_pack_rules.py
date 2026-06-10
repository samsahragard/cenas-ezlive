# all the base logic for shared math between the party packages
from __future__ import annotations

import logging
import math
from typing import List

from app.domain.schemas import TortillaChoice, PacketLineItem, KitchenLineItem, BeansChoice
from app.domain.rules_utils import make_weight_line
from app.domain.containers import container_for_oz

logger = logging.getLogger(__name__)

# add catering utensils calculations here

def per_person_rate(
    headcount: int,
    base_oz: float,
    above_30_oz: float | None = None,
    above_50_oz: float | None = None,
) -> float:
    if above_50_oz is not None and headcount > 50:
        return above_50_oz
    if above_30_oz is not None and headcount > 30:
        return above_30_oz
    return base_oz

def tortilla_packets(headcount: int, tortilla: TortillaChoice) -> list[PacketLineItem]:
    if tortilla in ("none", None):
        return []

    if headcount <= 0:
        return []

    total_packets = (headcount * 2.5) / 2

    if tortilla == "flour":
        return [{
            "name": "Flour Tortillas",
            "tortilla_type": "flour",
            "per_qty_packet": 2.5 / 2.0,
            "unit": "packets of 2",
            "packets": int(math.ceil(total_packets)),
            "raw_packets": total_packets,
        }]

    if tortilla == "corn":
        return [{
            "name": "Corn Tortillas",
            "tortilla_type": "corn",
            "per_qty_packet": 2.5 / 2.0,
            "unit": "packets of 3",
            "packets": int(math.ceil(total_packets)),
            "raw_packets": total_packets,
        }]

    if tortilla == "half_flour_half_corn":
        # Ceil the total first, then split to avoid double-rounding inflation.
        total_rounded = int(math.ceil(total_packets))
        flour_packets = total_rounded // 2
        corn_packets = total_rounded - flour_packets

        return [
            {
                "name": "Flour Tortillas",
                "tortilla_type": "flour",
                "per_qty_packet": 2.5 / 2.0,
                "unit": "packets of 2",
                "packets": flour_packets,
                "raw_packets": total_packets / 2,
            },
            {
                "name": "Corn Tortillas",
                "tortilla_type": "corn",
                "per_qty_packet": 2.5 / 2.0,
                "unit": "packets of 3",
                "packets": corn_packets,
                "raw_packets": total_packets / 2,
            },
        ]

    # Unrecognized tortilla value — return empty rather than crashing
    logger.warning("Unrecognized tortilla choice: %r", tortilla)
    return []

def _resolve_beans_choice(choice: BeansChoice) -> BeansChoice:
    if choice == "most_popular":
        return "refried"
    return choice

def party_sides(headcount: int, beans_choice: BeansChoice) -> list[KitchenLineItem]:
    if headcount <= 0:
        raise ValueError(f"party_sides called with invalid headcount: {headcount}")

    lines: list[KitchenLineItem] = []

    onions_pp = per_person_rate(headcount, base_oz=1.5, above_30_oz=1.0, above_50_oz=0.7)
    onions_total = onions_pp * headcount
    lines.append(make_weight_line("Onions", onions_total, onions_pp, "none"))

    pico_pp = per_person_rate(headcount, base_oz=1.5, above_30_oz=1.0)
    pico_total = pico_pp * headcount
    lines.append(make_weight_line("Pico De Gallo", pico_total, pico_pp, container_for_oz(pico_total)))

    guac_pp = per_person_rate(headcount, base_oz=1.5)
    guac_total = guac_pp * headcount
    lines.append(make_weight_line("Guacamole", guac_total, guac_pp, container_for_oz(guac_total)))

    sour_cream_pp = per_person_rate(headcount, base_oz=1.5, above_30_oz=1.0, above_50_oz=0.8)
    sour_cream_total = sour_cream_pp * headcount
    lines.append(make_weight_line("Sour Cream", sour_cream_total, sour_cream_pp, container_for_oz(sour_cream_total)))

    rice_pp = per_person_rate(headcount, base_oz=3.8, above_30_oz=3.5, above_50_oz=3.0)
    rice_total = rice_pp * headcount
    lines.append(make_weight_line("Rice", rice_total, rice_pp, container_for_oz(rice_total)))

    bc = _resolve_beans_choice(beans_choice)
    if bc != "none":
        beans_pp = per_person_rate(headcount, base_oz=3.8, above_30_oz=3.5, above_50_oz=3.0)
        beans_total = beans_pp * headcount
        beans_name = { 
            "refried": "Refried Beans",
            "charro": "Charro Beans",
            "black": "Black Beans",
        }.get(bc, "Beans")
        lines.append(make_weight_line(beans_name, beans_total, beans_pp, container_for_oz(beans_total)))

    chips_pp = per_person_rate(headcount, base_oz=3.0, above_30_oz=2.5, above_50_oz=2.3)
    chips_total = chips_pp * headcount
    lines.append(make_weight_line("Chips", chips_total, chips_pp, "none"))

    red_sauce_pp = per_person_rate(headcount, base_oz=1.5)
    red_sauce_total = red_sauce_pp * headcount
    lines.append(make_weight_line("Red Sauce", red_sauce_total, red_sauce_pp, container_for_oz(red_sauce_total)))

    green_sauce_pp = per_person_rate(headcount, base_oz=1.5)
    green_sauce_total = green_sauce_pp * headcount
    lines.append(make_weight_line("Green Sauce", green_sauce_total, green_sauce_pp, container_for_oz(green_sauce_total)))

    return lines


