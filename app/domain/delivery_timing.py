from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, TypedDict

KITCHEN_TO_DEPART_MINUTES = 15
KITCHEN_READY_BUFFER = 90
MULTI_STOP_BUFFER_MINUTES = 20


class SoloTimingResult(TypedDict):
    depart_store_at: str
    kitchen_ready_at: str
    travel_minutes: int


class RouteStopPlan(TypedDict):
    order_id: str
    stop_index: int
    eta: str
    latest_allowed_at: str
    minutes_late: int


class RoutePlanResult(TypedDict):
    feasible: bool
    depart_store_at: str
    pickup_at: str
    kitchen_ready_at: str
    total_drive_minutes: int
    stops: List[RouteStopPlan]
    flags: List[str]

# Turns time strings into proper datetime objects.
# Tries 12-hour with AM/PM first, falls back to bare HH:MM (treated as 24-hour).
def parse_datetime(date_str: str, time_str: str) -> datetime:
    combined = f"{date_str} {time_str}"
    for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(combined, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse time string: {time_str!r}")

# Formats datetime objects
def format_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")

# Calculates the difference in minutes between two datetime objects
def diff_minutes(a: datetime, b: datetime) -> int:
    return int((a - b).total_seconds() // 60)

# Calculates how many minutes late an ETA is compared to a deadline
def minutes_late(eta_dt: datetime, deadline_dt: datetime) -> int:
    if eta_dt > deadline_dt:
        return diff_minutes(eta_dt, deadline_dt)
    return 0


def compute_solo_timing(
    *,
    date_str: str,
    window_start: str,
    travel_minutes: int,
) -> SoloTimingResult:
    """
    window_start — earliest allowed arrival (start of delivery window)

    kitchen_ready_at = window_start - KITCHEN_READY_BUFFER - travel
    depart_store_at  = kitchen_ready_at + KITCHEN_TO_DEPART_MINUTES
    """
    window_start_dt = parse_datetime(date_str, window_start)
    kitchen_ready_dt = window_start_dt - timedelta(minutes=KITCHEN_READY_BUFFER + travel_minutes)

    return {
        "depart_store_at": format_time(kitchen_ready_dt + timedelta(minutes=KITCHEN_TO_DEPART_MINUTES)),
        "kitchen_ready_at": format_time(kitchen_ready_dt),
        "travel_minutes": travel_minutes,
    }



def compute_two_stop_route(
    *,
    origin_store_id_a: str,
    origin_store_id_b: str,
    date_str_a: str,
    date_str_b: str,
    first_order_id: str,
    first_deliver_by: str,
    first_window_start: str,
    store_to_first_minutes: int,
    second_order_id: str,
    second_deliver_by: str,
    second_window_start: str,
    first_to_second_minutes: int,
) -> RoutePlanResult:
    """
    Evaluates fixed sequence: store -> first stop -> second stop.

    Full chain:
        store -[DEPARTURE_BUFFER]-> pickup -[store_to_first]-> stop 1
              -[MULTI_STOP_BUFFER]-> -[first_to_second]-> stop 2

    Guards same store and same day before doing any math.
    Departure is the latest time that still satisfies both deadlines.

    kitchen_ready_at = min(
        first_window_start  - KITCHEN_READY_BUFFER - store_to_first,
        second_window_start - KITCHEN_READY_BUFFER - total_to_second,
    )
    """
    flags: List[str] = []

    if origin_store_id_a != origin_store_id_b:
        return {
            "feasible": False,
            "depart_store_at": "",
            "pickup_at": "",
            "kitchen_ready_at": "",
            "total_drive_minutes": 0,
            "stops": [],
            "flags": ["different origin stores"],
        }

    if date_str_a != date_str_b:
        return {
            "feasible": False,
            "depart_store_at": "",
            "pickup_at": "",
            "kitchen_ready_at": "",
            "total_drive_minutes": 0,
            "stops": [],
            "flags": ["different delivery dates"],
        }

    date_str = date_str_a

    first_deadline_dt = parse_datetime(date_str, first_deliver_by)
    second_deadline_dt = parse_datetime(date_str, second_deliver_by)

    # total_to_second includes the stop-1 service/traffic buffer so depart_dt
    # and kitchen_ready_dt are computed with the full chain in mind.
    total_to_second = store_to_first_minutes + MULTI_STOP_BUFFER_MINUTES + first_to_second_minutes
    depart_dt = min(
        first_deadline_dt - timedelta(minutes=store_to_first_minutes),
        second_deadline_dt - timedelta(minutes=total_to_second),
    )

    first_window_start_dt = parse_datetime(date_str, first_window_start)
    kitchen_ready_dt = min(
        first_window_start_dt - timedelta(minutes=KITCHEN_READY_BUFFER + store_to_first_minutes),
        parse_datetime(date_str, second_window_start) - timedelta(minutes=KITCHEN_READY_BUFFER + total_to_second),
    )

    first_eta_dt = depart_dt + timedelta(minutes=store_to_first_minutes)
    # If the driver arrives before stop 1's window opens they must wait,
    # so stop 2's ETA is based on the later of arrival and window open.
    first_service_start_dt = max(first_eta_dt, first_window_start_dt)
    second_eta_dt = first_service_start_dt + timedelta(minutes=MULTI_STOP_BUFFER_MINUTES + first_to_second_minutes)

    first_late = minutes_late(first_eta_dt, first_deadline_dt)
    second_late = minutes_late(second_eta_dt, second_deadline_dt)

    feasible = True

    if first_late > 0:
        feasible = False
        flags.append(f"first stop late by {first_late} min")

    if second_late > 0:
        feasible = False
        flags.append(f"second stop late by {second_late} min")

    return {
        "feasible": feasible,
        "depart_store_at": format_time(kitchen_ready_dt + timedelta(minutes=KITCHEN_TO_DEPART_MINUTES)),
        "pickup_at": format_time(depart_dt),
        "kitchen_ready_at": format_time(kitchen_ready_dt),
        "total_drive_minutes": total_to_second,
        "stops": [
            {
                "order_id": first_order_id,
                "stop_index": 1,
                "eta": format_time(first_eta_dt),
                "latest_allowed_at": first_deliver_by,
                "minutes_late": first_late,
            },
            {
                "order_id": second_order_id,
                "stop_index": 2,
                "eta": format_time(second_eta_dt),
                "latest_allowed_at": second_deliver_by,
                "minutes_late": second_late,
            },
        ],
        "flags": flags,
    }


def compute_best_two_stop_route(
    *,
    origin_store_id_a: str,
    origin_store_id_b: str,
    date_str_a: str,
    date_str_b: str,
    order_a_id: str,
    order_a_deliver_at: str,
    order_a_window_start: str,
    store_to_a_minutes: int,
    order_b_id: str,
    order_b_deliver_at: str,
    order_b_window_start: str,
    store_to_b_minutes: int,
    a_to_b_minutes: int,
    b_to_a_minutes: int,
) -> RoutePlanResult:
    """
    Tries both route orders (A->B and B->A) and returns the better feasible one.
    Preference: feasible > lower total lateness > lower total drive time.
    """
    route_ab = compute_two_stop_route(
        origin_store_id_a=origin_store_id_a,
        origin_store_id_b=origin_store_id_b,
        date_str_a=date_str_a,
        date_str_b=date_str_b,
        first_order_id=order_a_id,
        first_deliver_by=order_a_deliver_at,
        first_window_start=order_a_window_start,
        store_to_first_minutes=store_to_a_minutes,
        second_order_id=order_b_id,
        second_deliver_by=order_b_deliver_at,
        second_window_start=order_b_window_start,
        first_to_second_minutes=a_to_b_minutes,
    )

    route_ba = compute_two_stop_route(
        origin_store_id_a=origin_store_id_a,
        origin_store_id_b=origin_store_id_b,
        date_str_a=date_str_a,
        date_str_b=date_str_b,
        first_order_id=order_b_id,
        first_deliver_by=order_b_deliver_at,
        first_window_start=order_b_window_start,
        store_to_first_minutes=store_to_b_minutes,
        second_order_id=order_a_id,
        second_deliver_by=order_a_deliver_at,
        second_window_start=order_a_window_start,
        first_to_second_minutes=b_to_a_minutes,
    )

    def score(route: RoutePlanResult) -> tuple[int, int, int]:
        total_late = sum(stop["minutes_late"] for stop in route["stops"])
        return (0 if route["feasible"] else 1, total_late, route["total_drive_minutes"])

    return min([route_ab, route_ba], key=score)


def next_driver_name(index: int) -> str:
    """0 -> DRIVER A, 1 -> DRIVER B, ..., 26 -> DRIVER A #2"""
    letter = chr(ord("A") + index % 26)
    cycle = index // 26
    return f"DRIVER {letter}" if cycle == 0 else f"DRIVER {letter} #{cycle + 1}"
