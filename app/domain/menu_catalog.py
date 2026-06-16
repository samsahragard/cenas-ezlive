# internal menu dict + alias matching + item metadata
from __future__ import annotations
from typing import Optional, Dict, Any, List, Tuple, TypedDict, Literal

import re
from app.domain.schemas import PackageType

SheetSection = Literal[
    "HOT_FOOD", "COLD_FOOD", "PREMIUM",
    "A_LA_CARTE", "SIDES", "SALADS",
    "ENCHILADAS", "DRINKS_DESSERTS",
    "NON_FOOD", "OTHER"
]
SheetKind = Literal["qty", "lb", "packs", "packets"]

class SheetMeta(TypedDict, total=False):
    section: SheetSection
    label: str
    kind: SheetKind
    sort: int

class MenuItemMeta(TypedDict, total=False):
    alias: list[str]
    package_type: PackageType
    rule_params: dict
    sheet: SheetMeta

class MenuCatalog:
    def __init__(self, menu_data: Dict[str, Dict[str, Any]]):
        self.menu_data = menu_data
        self.alias_to_key = self._build_alias_index()

    def _norm(self, s: str) -> str:
        # Strip ezCater price tags like " @ $4.00" or "@ $125" so short item
        # names ("Churros", "Tamales", "Sodas") still match without falling
        # through to the 8-char substring fallback. Existing aliases never
        # contain "@ $" so this can't cause false matches.
        s = re.sub(r"\s*@\s*\$\d+(?:\.\d+)?\s*", "", s)
        return re.sub(r"\s+", " ", s).strip().lower()

    def _build_alias_index(self) -> Dict[str, str]:
        idx: Dict[str, str] = {}
        for item_key, meta in self.menu_data.items():
            for a in meta.get("alias", []):
                idx[self._norm(a)] = item_key
        return idx

    def get_item_key(self, raw_alias: str) -> Optional[str]:
        raw = self._norm(raw_alias)

        if raw in self.alias_to_key:
            return self.alias_to_key[raw]

        # Substring fallback: alias must be at least 8 chars to avoid false matches
        # (e.g. "beef" or "rice" matching wrong compound items)
        for alias, key in sorted(self.alias_to_key.items(), key=lambda x: len(x[0]), reverse=True):
            if len(alias) >= 8 and alias in raw:
                return key

        return None

    def get_package_type(self, item_key: str) -> Optional[PackageType]:
        meta = self.menu_data.get(item_key)
        return meta.get("package_type") if meta else None

    def get_sheet_meta(self, item_key: str) -> Optional[SheetMeta]:
        meta = self.menu_data.get(item_key)
        if not meta:
            return None
        return meta.get("sheet")
    
    def sheet_label(self, item_key: str) -> str:
        meta = self.menu_data.get(item_key) or {}
        sm = meta.get("sheet") or {}

        if "label" in sm and sm["label"]:
            return sm["label"]
        
        aliases = meta.get("alias") or []
        if aliases:
            return aliases[0]
        
        return item_key
    
    def sheet_section(self, item_key: str) -> Optional[SheetSection]:
        sm = self.get_sheet_meta(item_key) or {}
        return sm.get("section")
    
    def sheet_sort(self, item_key: str) -> int:
        sm = self.get_sheet_meta(item_key) or {}
        return int(sm.get("sort") or 9999)
    
    def get_component_sheet_meta(self, component_name: str) -> Optional[SheetMeta]:
        return SHEET_COMPONENTS.get(component_name)
    
    def component_sheet_label(self, component_name: str) -> str:
        meta = self.get_component_sheet_meta(component_name) or {}
        return meta.get("label", component_name)
    
    def component_sheet_section(self, component_name: str) -> Optional[SheetSection]:
         meta = self.get_component_sheet_meta(component_name) or {}
         return meta.get("section")
    
    def component_sheet_sort(self, component_name: str) -> int:
        meta = self.get_component_sheet_meta(component_name) or {}
        return int(meta.get("sort", 9999))

MENU_CATALOG = {
    "fajitas_mixed": {
        "alias": [
            "Beef & Chicken Fajita Party Package",
            "Beef and Chicken Fajita Party Package",
            "Beef & Chicken Fajitas Party Package",
            "Beef and Chicken Fajitas Party Package",
            "Beef & Chicken Fajita Package",
            "Mixed Fajita Party Package",
            "Chicken & Beef Fajita Party Package",
            "Chicken and Beef Fajita Party Package",
        ],
        "package_type": "fajitas",
        "sheet": {"section": "HOT_FOOD", "kind": "qty", "sort": 1},
        "produces": ["Chicken", "Beef", "Rice", "Beans", "Guacamole", "Pico De Gallo", "Sour Cream",
                     "Onions", "Red Sauce", "Green Sauce", "Chips", "Flour Tortillas", "Corn Tortillas"]
    },
    "fajitas_chicken": {
        "alias": [
            "Chicken Fajita Party Package",
            "Chicken Fajitas Party Package",
            "Chicken Fajita Package",
            "Chicken Fajitas Package",
        ],
        "package_type": "fajitas",
        "sheet": {"section": "HOT_FOOD", "kind": "qty", "sort": 2},
        "produces": ["Chicken", "Rice", "Beans", "Guacamole", "Pico De Gallo", "Sour Cream",
                     "Onions", "Red Sauce", "Green Sauce", "Chips", "Flour Tortillas", "Corn Tortillas"]
    },
    "fajitas_beef": {
        "alias": [
            "Beef Fajita Party Package",
            "Beef Fajitas Party Package",
            "Beef Fajita Package",
            "Beef Fajitas Package",
        ],
        "package_type": "fajitas",
        "sheet": {"section": "HOT_FOOD", "kind": "qty", "sort": 3},
        "produces": ["Beef", "Rice", "Beans", "Guacamole", "Pico De Gallo", "Sour Cream",
                     "Onions", "Red Sauce", "Green Sauce", "Chips", "Flour Tortillas", "Corn Tortillas"]
    },
    "veggie_fajitas": {
        "alias": [
            "Veggie Fajitas Party Package",
            "Veggie Fajita Party Package",
            "Veggie Fajitas Package",
            "Veggie Fajita Package",
            "Vegetarian Fajita Party Package",
            "Vegetable Fajita Party Package",
        ],
        "package_type": "veggie_fajitas",
        "sheet": {"section": "HOT_FOOD", "kind": "qty", "sort": 4},
        "produces": ["Veggie", "Rice", "Beans", "Guacamole", "Pico De Gallo", "Sour Cream",
                     "Onions", "Red Sauce", "Green Sauce", "Chips", "Flour Tortillas", "Corn Tortillas"]
    },
    "brochette_shrimp": {
        "alias": [
            "Brochette Shrimp Fajitas Package",
            "Brochette Shrimp Fajita Package",
            "Brochette Shrimp Package",
            "Shrimp Fajitas Party Package",
            "Shrimp Fajita Party Package",
            "Brochette Shrimp Party Package",
        ],
        "package_type": "brochette_shrimp",
        "sheet": {"section": "HOT_FOOD", "kind": "qty", "sort": 5},
        "produces": ["Shrimp (4-Pack)", "Rice", "Beans", "Guacamole", "Pico De Gallo", "Sour Cream",
                     "Onions", "Red Sauce", "Green Sauce", "Chips", "Flour Tortillas", "Corn Tortillas"]
    },
    "cenas_exec_spread": {
        "alias": [
            "Cenas Executive Fajita Spread",
            "Cenas Executive Fajitas Spread",
            "Cenas Executive Spread",
        ],
        "package_type": "premium",
        "sheet": {"section": "PREMIUM", "kind": "qty", "sort": 6},
    },
    "cobb_salad": {
        "alias": [
            "Cobb Salad Party Package",
            "Cobb Salad Package",
            "Cobb Salad",
        ],
        "package_type": "salads",
        "sheet": {"section": "SALADS", "kind": "qty", "sort": 10},
        "produces": {"Lettuce", "Avocado Diced", "Tomatoes Diced", "Cucumber Diced", "Grated Cheese", "Bacon", "Egg", "Black Olives", "Beef Diced", "Chicken Diced"}
    },
    "fajitas_and_salad": {
        "alias": [
            "Fajita Salad Party Package",
            "Fajita & Salad Party Package",
            "Fajitas and Salad Party Package",
            "Fajita Salad Package",
        ],
        "package_type": "salads",
        "sheet": {"section": "SALADS", "kind": "qty", "sort": 20},
        "produces": {"Lettuce", "Avocado Diced", "Tomatoes Diced", "Cucumber Diced", "Grated Cheese", "Beef", "Chicken"}
    },
    "cheese_enchiladas": {
        "alias": [
            "Cheese Enchiladas Tray (1 Dozen)",
            "Cheese Enchiladas Tray",
            "Cheese Enchiladas (1 Dozen)",
            "Cheese Enchiladas - Tray",
            "Cheese Enchilada Tray",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 10}
    },
    "shredded_chicken_enchiladas": {
        "alias": [
            "Shredded Chicken Enchiladas Tray (1 Dozen)",
            "Shredded Chicken Enchiladas Tray",
            "Shredded Chicken Enchiladas (1 Dozen)",
            "Shredded Chicken Enchiladas - Tray",
            "Shredded Chicken Enchilada Tray",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 20}
    },
    "ground_beef_enchiladas": {
        "alias": [
            "Ground Beef Enchiladas Tray (1 Dozen)",
            "Ground Beef Enchiladas Tray",
            "Ground Beef Enchiladas (1 Dozen)",
            "Ground Beef Enchiladas - Tray",
            "Ground Beef Enchilada Tray",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 30}
    },
    "veggie_enchiladas": {
        "alias": [
            "Veggie Enchiladas Tray (1 Dozen)",
            "Veggie Enchiladas Tray",
            "Veggie Enchiladas (1 Dozen)",
            "Veggie Enchiladas - Tray",
            "Vegetarian Enchiladas Tray",
            "Vegetable Enchiladas Tray",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 40}
    },
    "beef_enchiladas": {
        "alias": [
            "Beef Fajita Enchiladas Tray (1 Dozen)",
            "Beef Fajita Enchiladas Tray",
            "Beef Fajita Enchiladas (1 Dozen)",
            "Beef Fajita Enchiladas - Tray",
            "Beef Enchiladas Tray",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 50}
    },
    "chicken_enchiladas": {
        "alias": [
            "Chicken Fajita Enchiladas Tray (1 Dozen)",
            "Chicken Fajita Enchiladas Tray",
            "Chicken Fajita Enchiladas (1 Dozen)",
            "Chicken Fajita Enchiladas - Tray",
            "Chicken Enchiladas Tray",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 60}
    },
    "pork_enchiladas": {
        "alias": [
            "Pork Enchiladas Tray (1 Dozen)",
            "Pork Enchiladas Tray",
            "Pork Enchiladas (1 Dozen)",
            "Pork Enchiladas - Tray",
            "Pork Enchilada Tray",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 70}
    },
    "seafood_enchiladas": {
        "alias": [
            "Seafood Enchiladas Tray (1 Dozen)",
            "Seafood Enchiladas Tray",
            "Seafood Enchiladas (1 Dozen)",
            "Seafood Enchiladas - Tray",
            "Seafood Enchilada Tray",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 80}
    },
    "tamales": {
        "alias": [
            "Tamales",
            "Tamale Tray",
            "Tamales Tray",
            "Tamales (Tray)",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 90}
    },
    "veggie_enchiladas_individual": {
        "alias": [
            "Veggie Enchiladas (Individually Packaged)",
            "Veggie Enchiladas (Individual)",
            "Veggie Enchiladas Individually Packaged",
            "Veggie Enchiladas Individual",
            "Veggie Enchiladas (Individually Packed)",
            "Vegetarian Enchiladas (Individually Packaged)",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 100}
    },
    "beef_enchiladas_individual": {
        "alias": [
            "Beef Fajita Enchiladas (Individually Packaged)",
            "Beef Fajita Enchiladas (Individual)",
            "Beef Fajita Enchiladas Individually Packaged",
            "Beef Fajita Enchiladas Individual",
            "Beef Enchiladas (Individually Packaged)",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 110}
    },
    "chicken_enchiladas_individual": {
        "alias": [
            "Chicken Enchiladas (Individually Packaged)",
            "Chicken Enchiladas (Individual)",
            "Chicken Enchiladas Individually Packaged",
            "Chicken Enchiladas Individual",
            "Chicken Enchiladas (Individually Packed)",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 120}
    },
    "chicken_fajita_enchiladas_individual": {
        "alias": [
            "Chicken Fajita Enchiladas (Individually Packaged)",
            "Chicken Fajita Enchiladas (Individual)",
            "Chicken Fajita Enchiladas Individually Packaged",
            "Chicken Fajita Enchiladas Individual",
            "Chicken Fajita Enchiladas (Individually Packed)",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 130}
    },
    "cheese_enchiladas_individual": {
        "alias": [
            "Cheese Enchiladas (Individually Packaged)",
            "Cheese Enchiladas (Individual)",
            "Cheese Enchiladas Individually Packaged",
            "Cheese Enchiladas Individual",
            "Cheese Enchiladas (Individually Packed)",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 140}
    },
    "ground_beef_enchiladas_individual": {
        "alias": [
            "Ground Beef Enchiladas (Individually Packaged)",
            "Ground Beef Enchiladas (Individual)",
            "Ground Beef Enchiladas Individually Packaged",
            "Ground Beef Enchiladas Individual",
            "Ground Beef Enchiladas (Individually Packed)",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 150}
    },
    "pork_enchiladas_individual": {
        "alias": [
            "Pork Enchiladas (Individually Packaged)",
            "Pork Enchiladas (Individual)",
            "Pork Enchiladas Individually Packaged",
            "Pork Enchiladas Individual",
            "Pork Enchiladas (Individually Packed)",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 160}
    },
    "seafood_enchiladas_individual": {
        "alias": [
            "Seafood Enchiladas (Individually Packaged)",
            "Seafood Enchiladas (Individual)",
            "Seafood Enchiladas Individually Packaged",
            "Seafood Enchiladas Individual",
            "Seafood Enchiladas (Individually Packed)",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 170}
    },
    "tamales_individual": {
        "alias": [
            "Tamales (Individually Packaged)",
            "Tamales (Individual)",
            "Tamales Individually Packaged",
            "Tamales Individual",
            "Tamales (Individually Packed)",
        ],
        "package_type": "enchiladas",
        "sheet": {"section": "ENCHILADAS", "kind": "qty", "sort": 180}
    },
    "jumbo_brochette_shrimp": {
        "alias": [
            "Jumbo Brochette Shrimp",
            "Jumbo Brochette",
            "Brochette Shrimp",
        ],
        "package_type": "a_la_carte",
        "sheet": {"section": "A_LA_CARTE", "kind": "qty", "sort": 10}
    },
    "andouille_grilled_sausage": {
        "alias": [
            "Andouille Grilled Sausage",
            "Andouille Sausage",
            "Grilled Andouille Sausage",
        ],
        "package_type": "a_la_carte",
        "sheet": {"section": "A_LA_CARTE", "kind": "qty", "sort": 20}
    },
    "baja_ribs": {
        "alias": [
            "Baja Baby Back Ribs",
            "Baja Ribs",
            "Baby Back Ribs",
            "Baja Baby Back Rib",
        ],
        "package_type": "a_la_carte",
        "sheet": {"section": "A_LA_CARTE", "kind": "qty", "sort": 30}
    },
    "beef_faj_per_pound": {
        "alias": [
            "Beef Fajita Per Pound",
            "Beef Fajitas Per Pound",
            "Beef Fajita by the Pound",
        ],
        "package_type": "a_la_carte",
        "sheet": {"section": "A_LA_CARTE", "kind": "qty", "sort": 40},
        "produces": ["Beef"]
    },
    "chicken_faj_per_pound": {
        "alias": [
            "Chicken Fajita Per Pound",
            "Chicken Fajitas Per Pound",
            "Chicken Fajita by the Pound",
        ],
        "package_type": "a_la_carte",
        "sheet": {"section": "A_LA_CARTE", "kind": "qty", "sort": 50},
        "produces": ["Chicken"]
    },
    "queso_and_chips": {
        "alias": [
            "Queso & Chips",
            "Queso and Chips",
            "Queso Dip & Chips",
            "Queso Dip and Chips",
            "Queso",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 10}
    },
    "guac_and_chips": {
        "alias": [
            "Guacamole & Chips",
            "Guacamole and Chips",
            "Guac & Chips",
            "Guac and Chips",
            "Guacamole",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 20}
    },
    "rice": {
        "alias": [
            "Rice",
            "Spanish Rice",
            "Mexican Rice",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 30}
    },
    "refried_beans": {
        "alias": [
            "Refried Beans",
            "Refried Bean",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 40}
    },
    "grated_cheese": {
        "alias": [
            "Grated Cheese",
            "Shredded Cheese",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 50}
    },
    "charro_beans": {
        "alias": [
            "Charro Beans",
            "Charro Bean",
            "Frijoles Charros",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 60}
    },
    "pickled_jalapenos": {
        "alias": [
            "Pickled Jalapeños",
            "Pickled Jalapenos",
            "Pickled Jalapeño",
            "Pickled Jalapeno",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 70}
    },
    "sour_cream": {
        "alias": [
            "Sour Cream",
            "Crema",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 80}
    },
    "red_sauce": {
        "alias": [
            "Red Sauce",
            "Salsa Roja",
            "Hot Sauce",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 90}
    },
    "pico_de_gallo": {
        "alias": [
            "Pico De Gallo",
            "Pico de Gallo",
            "Pico",
            "Fresh Salsa",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 100}
    },
    "green_sauce": {
        "alias": [
            "Green Sauce",
            "Salsa Verde",
            "Tomatillo Sauce",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 110}
    },
    "fresh_avocado": {
        "alias": [
            "Fresh Avocado",
            "Avocado",
            "Sliced Avocado",
            "Diced Avocado",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 120}
    },
    "flour_tort": {
        "alias": [
            "Flour Tortillas",
            "Flour Tortilla",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 130},
        "produces": ["Flour Tortillas"]
    },
    "corn_tort": {
        "alias": [
            "Corn Tortillas",
            "Corn Tortilla",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 135},
        "produces": ["Corn Tortillas"]
    },
    "black_beans": {
        "alias": [
            "Black Beans",
            "Black Bean",
            "Frijoles Negros",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 140}
    },
    "fresh_jalapenos": {
        "alias": [
            "Fresh Jalapeños",
            "Fresh Jalapenos",
            "Fresh Jalapeño",
            "Fresh Jalapeno",
        ],
        "package_type": "sides",
        "sheet": {"section": "SIDES", "kind": "qty", "sort": 150}
    },
    "churros": {
        "alias": [
            "Churros",
            "Churro",
        ],
        "package_type": "desserts",
        "sheet": {"section": "DRINKS_DESSERTS", "kind": "qty", "sort": 10}
    },
    "sopapillas": {
        "alias": [
            "Sopapillas",
            "Sopapilla",
            "Sopaipillas",
        ],
        "package_type": "desserts",
        "sheet": {"section": "DRINKS_DESSERTS", "kind": "qty", "sort": 20}
    },
    "tres_leches": {
        "alias": [
            "Tres Leches",
            "Tres Leches Cake",
        ],
        "package_type": "desserts",
        "sheet": {"section": "DRINKS_DESSERTS", "kind": "qty", "sort": 30}
    },
    "gallon_unsweet_tea": {
        "alias": [
            "Gallon Unsweet Tea",
            "Gallon Unsweetened Tea",
            "Unsweet Tea (Gallon)",
            "Unsweetened Tea (Gallon)",
            "Gallon of Unsweet Tea",
        ],
        "package_type": "beverages",
        "sheet": {"section": "DRINKS_DESSERTS", "kind": "qty", "sort": 40}
    },
    "gallon_sweet_tea": {
        "alias": [
            "Gallon Sweet Tea",
            "Gallon Sweetened Tea",
            "Sweet Tea (Gallon)",
            "Gallon of Sweet Tea",
        ],
        "package_type": "beverages",
        "sheet": {"section": "DRINKS_DESSERTS", "kind": "qty", "sort": 50}
    },
    "lemonade": {
        "alias": [
            "Lemonade",
            "Gallon Lemonade",
            "Lemonade (Gallon)",
        ],
        "package_type": "beverages",
        "sheet": {"section": "DRINKS_DESSERTS", "kind": "qty", "sort": 60}
    },
    "sodas": {
        "alias": [
            "20oz Bottled Sodas",
            "20 oz Bottled Sodas",
            "Bottled Sodas",
            "20oz Soda",
            "20oz Sodas",
            "Assorted Sodas",
        ],
        "package_type": "beverages",
        "sheet": {"section": "DRINKS_DESSERTS", "kind": "qty", "sort": 70}
    },
    "water": {
        "alias": [
            "Bottled Waters",
            "Bottled Water",
            "Water Bottles",
            "Water Bottle",
        ],
        "package_type": "beverages",
        "sheet": {"section": "DRINKS_DESSERTS", "kind": "qty", "sort": 80}
    },
    "jarritos_sodas": {
        "alias": [
            "12oz Jarritos Sodas",
            "12 oz Jarritos Sodas",
            "Jarritos",
            "Jarritos Sodas",
            "Jarritos (12oz)",
        ],
        "package_type": "beverages",
        "sheet": {"section": "DRINKS_DESSERTS", "kind": "qty", "sort": 90}
    },
    "tableware": {
        "alias": [
            "Tableware",
            "Tableware Package",
            "Utensils",
            "Utensils & Tableware",
            "Utensils and Tableware",
            "Napkins",
            "Plates/Bowls",
            "Cups",
        ],
        "package_type": "non_food_items",
        "sheet": {"section": "NON_FOOD", "kind": "qty", "sort": 10}
    },
    "plates_and_bowls": {
        "alias": [
            "Plates/Bowls",
            "Plates & Bowls",
            "Plates and Bowls",
            "Plates",
        ],
        "package_type": "non_food_items",
        "sheet": {"section": "NON_FOOD", "kind": "qty", "sort": 15}
    },
    "chafing_dish_set": {
        "alias": [
            "Complete Catering Chafing Dish Set",
            "Chafing Dish Set",
            "Catering Chafing Dish Set",
            "Chafing Dish",
        ],
        "package_type": "non_food_items",
        "sheet": {"section": "NON_FOOD", "kind": "qty", "sort": 20}
    }
}

SHEET_COMPONENTS = {
     "Chicken": {
         "section": "HOT_FOOD", 
         "label": "Chicken (Lb)", 
         "kind": "lb", 
         "sort": 1
     },
     "Beef": {
         "section": "HOT_FOOD", 
         "label": "Beef (Lb)", 
         "kind": "lb", 
         "sort": 2
     },
     "Veggie": {
         "section": "HOT_FOOD", 
         "label": "Veggies (Lb)", 
         "kind": "lb", 
         "sort": 3
     },
     "Shrimp (4-Pack)": {
         "section": "HOT_FOOD", 
         "label": "Brochette Shrimp (4-pack)", 
         "kind": "packs", 
         "sort": 4
     },
     "Rice": {
         "section": "HOT_FOOD", 
         "label": "Rice (Lb)", 
         "kind": "lb", 
         "sort": 10
     },
     "Refried Beans": {
         "section": "HOT_FOOD", 
         "label": "Beans (Lb)", 
         "kind": "lb", 
         "sort": 20
     },
     "Charro Beans": {
         "section": "HOT_FOOD", 
         "label": "Beans (Lb)", 
         "kind": "lb", 
         "sort": 20
     },
     "Black Beans": {
         "section": "HOT_FOOD", 
         "label": "Beans (Lb)", 
         "kind": "lb", 
         "sort": 20
     },
     "Flour Tortillas": {
         "section": "HOT_FOOD", 
         "label": "Flour Tortillas (pkts of 2)", 
         "kind": "packets", 
         "sort": 30
     },
     "Corn Tortillas": {
         "section": "HOT_FOOD", 
         "label": "Corn Tortillas (pkts of 3)", 
         "kind": "packets", 
         "sort": 30
     },
     # COLD FOOD
     "Onions": {
         "section": "COLD_FOOD", 
         "label": "Onions (Lb)", 
         "kind": "lb", 
         "sort": 40
     },
     "Pico De Gallo": {
         "section": "COLD_FOOD", 
         "label": "Pico De Gallo (Lb)", 
         "kind": "lb", 
         "sort": 50
     },
     "Guacamole": {
         "section": "COLD_FOOD", 
         "label": "Guacamole (Lb)", 
         "kind": "lb", 
         "sort": 60
     },
     "Queso Blanco": {
         "section": "COLD_FOOD",
         "label": "Queso Blanco (Lb)",
         "kind": "lb",
         "sort": 65
     },
     "Sour Cream": {
         "section": "COLD_FOOD", 
         "label": "Sour Cream (Lb)", 
         "kind": "lb", 
         "sort": 70
     },
     "Lettuce": {
         "section": "COLD_FOOD",
         "label": "Lettuce (Lb)",
         "kind": "lb",
         "sort": 71
     },
     "Avocado Diced": {
         "section": "COLD_FOOD",
         "label": "Diced Avocado (Lb)",
         "kind": "lb",
         "sort": 72
     },
     "Tomatoes Diced": {
         "section": "COLD_FOOD",
         "label": "Diced Tomatoes (Lb)",
         "kind": "lb",
         "sort": 73
     },
     "Cucumber Diced": {
         "section": "COLD_FOOD",
         "label": "Diced Cucumber (Lb)",
         "kind": "lb",
         "sort": 74
     },
     "Grated Cheese": {
         "section": "COLD_FOOD",
         "label": "Grated Cheese (Lb)",
         "kind": "lb",
         "sort": 75
     },
     "Bacon": {
         "section": "COLD_FOOD",
         "label": "Bacon (Lb)",
         "kind": "lb",
         "sort": 76
     },
     "Egg": {
         "section": "COLD_FOOD",
         "label": "Egg (Lb)",
         "kind": "lb",
         "sort": 77
     },
     "Black Olives": {
         "section": "COLD_FOOD",
         "label": "Black Olives (Lb)",
         "kind": "lb",
         "sort": 78
     },
     "Beef Diced": {
         "section": "COLD_FOOD",
         "label": "Diced Beef (Lb)",
         "kind": "lb",
         "sort": 79
     },
     "Chicken Diced": {
         "section": "COLD_FOOD",
         "label": "Chicken Diced (Lb)",
         "kind": "lb",
         "sort": 80
     },
     "Red Sauce": {
         "section": "COLD_FOOD", 
         "label": "Red Sauce (Lb)", 
         "kind": "lb", 
         "sort": 90
     },
     "Green Sauce": {
         "section": "COLD_FOOD", 
         "label": "Green Sauce (Lb)", 
         "kind": "lb", 
         "sort": 100
     },
     "Chips": {
         "section": "COLD_FOOD", 
         "label": "Chips (Lb)", 
         "kind": "lb", 
         "sort": 110
     },
     "Churros": {
         "section": "DRINKS_DESSERTS",
         "label": "Churros (Pieces)",
         "kind": "qty",
         "sort": 120
     }
          
}
