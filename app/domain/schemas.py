# TypedDict/dataclasses: RawOrder, NormalizedOrder, PrepBreakdown, etc.
from __future__ import annotations
from typing import TypedDict, List, Optional, Literal, Dict, Union
from datetime import datetime
 
# normalized conversion defining
PackageType = Literal["fajitas", "veggie_fajitas", "brochette_shrimp", "premium", "enchiladas", "sides", "desserts", "salads", "a_la_carte", "beverages", "non_food_items", "other"]
PackagingType = Literal["tray", "individual", "none"]
BeansChoice = Literal["refried", "charro", "black", "most_popular", "none"]
TortillaChoice = Literal["flour", "corn", "half_flour_half_corn", "none"]
MeasureType = Literal["weight", "count"]  

OriginStoreId = Literal["store_1", "store_2"]
DriverId = str

#defining line items
class ItemChoices(TypedDict):
    packaging: PackagingType
    beans: BeansChoice
    tortillas: TortillaChoice

    with_ice: Optional[bool]

# avoid using datetime for now
class TimeWindow(TypedDict):
    start: str
    end: str

# raw item -> raw order defining the structure of data and then organizing it
class RawItem(TypedDict):
    alias: str
    qty: int
    line_items: List[str]

class RawOrder(TypedDict):
    order_id: str

    client: str
    upon_delivery_ask_for: str
    store: str
    headcount: int
    customer_phone: str

    date: str
    deliver_at: str
    delivery_window: TimeWindow
    delivery_address: str
    delivery_instructions: Optional[str]

    setup_required: Optional[bool] # change later
    notes: Optional[str]

    raw_items: List[RawItem]

    extraction_confidence: Literal["high", "medium", "low"]
    uncertain_fields: List[str]
    extraction_notes: Optional[str]

class ExtraLineItem(TypedDict):
    name: str
    raw_text: str

# normalized item -> normalized order defining the structure of data and then organizing it

class ItemSource(TypedDict):
    raw_alias: str
    raw_qty: int
    raw_line_items: List[str]

class NormalizedItem(TypedDict):
    item_key: str
    package_type: PackageType
    qty: int
    choices: ItemChoices
    extras: List[ExtraLineItem]
    container: Optional[str]

    source: ItemSource

    flags: List[str]

class NormalizedOrder(TypedDict):
    order_id: str

    client: str
    upon_delivery_ask_for: str
    reported_store: str
    reported_store_id: str
    origin_store_id: str
    headcount: int
    customer_phone: str

    date: str
    deliver_at: str
    delivery_window: TimeWindow
    delivery_address: str
    delivery_instructions: Optional[str]

    setup_required: Optional[bool]
    notes: Optional[str]

    normalized_items: List[NormalizedItem]
    flags: List[str]

    route_group_id: Optional[str]
    route_stop_index: Optional[int]
    assigned_driver: Optional[str]

class KitchenLineItem(TypedDict):
    name: str
    measure_type: MeasureType
    per_qty: Optional[float]
    unit: str
    total: float
    display_total: str

# Packet math
class PacketLineItem(TypedDict):
    name: str
    tortilla_type: Optional[TortillaChoice]
    per_qty_packet: Optional[float]
    unit: str
    packets: int
    raw_packets: float

# Output of prep breakdown
class PrepBreakdown(TypedDict):
    item_key: str
    package_type: PackageType
    qty: int

    choices: ItemChoices

    proteins: List[KitchenLineItem]
    sides: List[KitchenLineItem]
    sauces: List[KitchenLineItem]
    extras: List[ExtraLineItem]
    utensil_sub_counts: Optional[Dict[str, int]]

    counts: List[PacketLineItem]

    summary_line: Optional[str]

    flags: List[str]

# Full kitchen ticket
class KitchenOrderResult(TypedDict):
    kitchen_ready_time: str
    order_id: str
    
    date: str
    store: str

    breakdowns: List[PrepBreakdown]
    flags: List[str]

    kitchen_ticket_text: str

class OrderTiming(TypedDict):
    order_id: str
    origin_store_id: OriginStoreId

    travel_minutes: int

    solo_depart_store_at: str
    solo_pickup_at: str
    solo_kitchen_done_at: str

    flags: List[str]

class RouteStop(TypedDict):
    order_id: str
    stop_index: int
    delivery_address: str

    eta: str
    window_start : str
    delivery_window: TimeWindow


class DriverRoute(TypedDict):
    route_group_id: str
    origin_store_id: OriginStoreId
    assigned_driver: Optional[DriverId]

    depart_store_at: str
    pickup_at: str
    kitchen_done_at: str

    total_drive_minutes: int
    stops: List[RouteStop]

    flags: List[str]
