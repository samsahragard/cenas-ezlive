"""In-House Catering menu — Cenas Fajitas (Tomball ezCater mirror).

Source: scraped from https://www.ezcater.com/catering/cenas-fajitas-tomball
by ck via Chrome MCP 2026-05-18. See repo `/c/Users/sam/ezcater_cenas_tomball_menu.md`
for the original source-of-truth markdown dump.

Per Sam #837 item 16 + cena #1031 (2026-05-19) directive:
- Tray Packaging / Individual Packaging modifier options OMITTED.
- "Reference price" is the ezCater price; In-House quotes default to $0
  (staff enters custom price during quote creation).
- "Modifier groups" follow the per-category pattern documented at the
  bottom of the source markdown file.

Shape:
    CATEGORIES = [
        {
            "slug": "...",
            "label": "...",
            "modifier_groups": [...],   # shared across items in category
            "items": [
                {"slug": "...", "label": "...", "reference_price": 0.00,
                 "min_qty": 4, "serves": "...", "description": "...",
                 "most_ordered": True, "item_modifiers": [...]},
                ...
            ],
        },
        ...
    ]

Modifier-group shape:
    {"slug": "...", "label": "...", "required": True, "max_select": 1,
     "options": [{"label": "...", "upcharge": 0.49}, ...]}
"""

# Shared modifier groups reused across categories.
_TORTILLA_GROUP = {
    "slug": "tortillas",
    "label": "Select tortillas",
    "required": True,
    "max_select": 1,
    "options": [
        {"label": "Flour Tortillas",                       "upcharge": 0.00},
        {"label": "Corn Tortillas",                        "upcharge": 0.00},
        {"label": "Half Flour & Half Corn Tortillas",      "upcharge": 0.49},
    ],
}

_BEANS_GROUP = {
    "slug": "beans",
    "label": "Select beans",
    "required": True,
    "max_select": 1,
    "options": [
        {"label": "Most Popular (restaurant picks)",       "upcharge": 0.00},
        {"label": "Charro Beans",                          "upcharge": 0.00},
        {"label": "Black Beans",                           "upcharge": 0.00},
        {"label": "Refried Beans",                         "upcharge": 0.00},
    ],
}

_ENCHILADA_SAUCE_GROUP = {
    "slug": "sauce",
    "label": "Select sauce",
    "required": True,
    "max_select": 1,
    "options": [
        {"label": "Most popular (restaurant picks)",       "upcharge": 0.00},
        {"label": "No Sauce",                              "upcharge": 0.00},
        {"label": "Queso Sauce",                           "upcharge": 0.00},
        {"label": "Tomatillo Sauce",                       "upcharge": 0.00},
        {"label": "Ranchero Sauce",                        "upcharge": 0.00},
        {"label": "Poblano Sauce",                         "upcharge": 0.00},
        {"label": "Street Taco Sauce",                     "upcharge": 0.00},
    ],
}

_ALACARTE_SIZE_GROUP = {
    "slug": "size",
    "label": "Select size",
    "required": True,
    "max_select": 1,
    "options": [
        {"label": "Half Pound", "upcharge": 0.00},
        {"label": "Pound",      "upcharge": 0.00},
    ],
}

_SIDE_SIZE_GROUP = {
    "slug": "size",
    "label": "Select size",
    "required": True,
    "max_select": 1,
    "options": [
        {"label": "Pint (serves 10)",  "upcharge": 0.00},
        {"label": "Quart (serves 20)", "upcharge": 0.00},
    ],
}


CATEGORIES = [
    {
        "slug": "premium-packages",
        "label": "Premium Catering Packages",
        "modifier_groups": [],
        "items": [
            {
                "slug": "executive-fajita-spread",
                "label": "Cenas Executive Fajita Spread",
                "reference_price": 28.39,
                "serves": "per person",
                "most_ordered": True,
                "description": "All-inclusive premium package. Bundled queso, Fajita & Salad Bar, Churros. Queso Blanco + chips + red & green salsas to start. Fajita Bar (beef + chicken, grilled onions, guacamole, pico, sour cream, flour tortillas, rice, charro beans). Salad bar. Churros to close.",
            },
        ],
    },
    {
        "slug": "catering-packages",
        "label": "Catering Packages",
        "modifier_groups": [_TORTILLA_GROUP, _BEANS_GROUP],
        "items": [
            {"slug": "beef-chicken-fajita-party",   "label": "Beef & Chicken Fajita Party Package", "reference_price": 21.49, "min_qty": 4, "serves": "per person", "most_ordered": True,
             "description": "Build-your-own fajitas: chicken & beef, grilled onions, guacamole, pico, sour cream, tortillas of choice. Rice, beans of choice, red & green sauce, corn tortilla chips."},
            {"slug": "veggie-fajita-party",         "label": "Veggie Fajitas Party Package",        "reference_price": 17.99, "min_qty": 4, "serves": "per person", "most_ordered": True,
             "description": "Build-your-own veggie fajitas: sauteed vegetables, guacamole, pico, sour cream, tortillas of choice. Rice, black beans, red & green sauce, corn tortilla chips."},
            {"slug": "chicken-fajita-party",        "label": "Chicken Fajita Party Package",        "reference_price": 19.89, "min_qty": 4, "serves": "per person", "most_ordered": True,
             "description": "Build-your-own chicken fajitas: chicken, grilled onions, guacamole, pico, sour cream, tortillas of choice. Rice, beans of choice, red & green sauce, corn tortilla chips."},
            {"slug": "beef-fajita-party",           "label": "Beef Fajita Party Package",           "reference_price": 23.69, "min_qty": 4, "serves": "per person",
             "description": "Build-your-own beef fajitas: beef, grilled onions, guacamole, pico, sour cream, tortillas of choice. Rice, beans of choice, red & green sauce, corn tortilla chips."},
            {"slug": "brochette-shrimp-fajita",     "label": "Brochette Shrimp Fajitas Package",    "reference_price": 19.89, "min_qty": 4, "serves": "per person",
             "description": "Brochette shrimp fajitas wrapped in bacon, stuffed with Monterey cheese & peppers. Pineapple butter, guacamole, sour cream, pico, beans of choice, tortillas of choice, Mexican rice."},
        ],
    },
    {
        "slug": "salad-packages",
        "label": "Salad Catering Packages",
        "modifier_groups": [],
        "items": [
            {"slug": "cobb-salad-party",   "label": "Cobb Salad Party Package",   "reference_price": 14.99, "serves": "per person",
             "description": "Mixed greens, fajita protein of choice, crispy bacon, cheese, avocado, tomatoes, black olives, boiled egg. 3 dressings.",
             "item_modifiers": [
                 {"slug": "protein", "label": "Select protein", "required": True, "max_select": 1, "options": [
                     {"label": "Most Popular (restaurant picks)", "upcharge": 0.00},
                     {"label": "Chicken Fajitas",                 "upcharge": 0.00},
                     {"label": "Beef Fajitas",                    "upcharge": 3.50},
                     {"label": "Mix Fajitas",                     "upcharge": 2.00},
                 ]},
                 {"slug": "dressing-3", "label": "Select 3 dressings", "required": True, "max_select": 3, "min_select": 1, "options": [
                     {"label": "Most Popular",        "upcharge": 0.00},
                     {"label": "Ranch Dressing",      "upcharge": 0.00},
                     {"label": "Italian Dressing",    "upcharge": 0.00},
                     {"label": "Sweet Ginger",        "upcharge": 0.00},
                     {"label": "Honey Mustard",       "upcharge": 0.00},
                     {"label": "Red Sauce",           "upcharge": 0.00},
                     {"label": "Green Sauce",         "upcharge": 0.00},
                     {"label": "Queso Dressing",      "upcharge": 0.49},
                 ]},
             ]},
            {"slug": "fajita-salad-party", "label": "Fajita Salad Party Package", "reference_price": 17.99, "serves": "per person",
             "description": "Mixed greens, Roma tomatoes, red onions, grated mix cheeses, cucumbers, avocado, tortilla strips. 2 dressings.",
             "item_modifiers": [
                 {"slug": "protein", "label": "Select protein", "required": True, "max_select": 1, "options": [
                     {"label": "Most Popular (restaurant picks)", "upcharge": 0.00},
                     {"label": "Chicken Fajitas",                 "upcharge": 0.00},
                     {"label": "Beef Fajitas",                    "upcharge": 3.50},
                     {"label": "Mix Fajitas",                     "upcharge": 2.00},
                 ]},
                 {"slug": "dressing-2", "label": "Select 2 dressings", "required": True, "max_select": 2, "min_select": 1, "options": [
                     {"label": "Most Popular",        "upcharge": 0.00},
                     {"label": "Ranch Dressing",      "upcharge": 0.00},
                     {"label": "Italian Dressing",    "upcharge": 0.00},
                     {"label": "Sweet Ginger",        "upcharge": 0.00},
                     {"label": "Honey Mustard",       "upcharge": 0.00},
                     {"label": "Red Sauce",           "upcharge": 0.00},
                     {"label": "Green Sauce",         "upcharge": 0.00},
                 ]},
             ]},
        ],
    },
    {
        "slug": "enchilada-trays",
        "label": "Enchilada Trays (does not include chips & salsa)",
        "modifier_groups": [_ENCHILADA_SAUCE_GROUP],
        "items": [
            {"slug": "cheese-enchiladas-tray",          "label": "Cheese Enchiladas Tray (1 Dozen)",          "reference_price": 30.00, "serves": "6", "most_ordered": True,
             "description": "Corn tortillas filled with cheese, covered in sauce-of-choice & grated cheese."},
            {"slug": "shredded-chicken-enchiladas-tray","label": "Shredded Chicken Enchiladas Tray (1 Dozen)","reference_price": 34.00, "serves": "6", "most_ordered": True},
            {"slug": "ground-beef-enchiladas-tray",     "label": "Ground Beef Enchiladas Tray (1 Dozen)",     "reference_price": 33.00, "serves": "6"},
            {"slug": "veggie-enchiladas-tray",          "label": "Veggie Enchiladas Tray (1 Dozen)",          "reference_price": 38.00, "serves": "6",
             "description": "Corn tortillas filled with sauteed vegetables."},
            {"slug": "beef-fajita-enchiladas-tray",     "label": "Beef Fajita Enchiladas Tray (1 Dozen)",     "reference_price": 44.00, "serves": "6",
             "description": "Corn tortillas filled with grilled beef fajitas."},
            {"slug": "chicken-fajita-enchiladas-tray",  "label": "Chicken Fajita Enchiladas Tray (1 Dozen)",  "reference_price": 40.00, "serves": "6",
             "description": "Corn tortillas filled with grilled chicken fajitas."},
            {"slug": "tamales-tray",                    "label": "Tamales (1 Dozen)",                          "reference_price": 41.00, "serves": "6",
             "description": "Steamed corn husk stuffed with cornmeal and chili con carne."},
            {"slug": "seafood-enchiladas-tray",         "label": "Seafood Enchiladas Tray (1 Dozen)",          "reference_price": 43.00, "serves": "6",
             "description": "Filled with sauteed shrimp & crawfish, seafood sauce."},
            {"slug": "pork-enchiladas-tray",            "label": "Pork Enchiladas Tray (1 Dozen)",             "reference_price": 33.00, "serves": "6",
             "description": "Shredded pork."},
        ],
    },
    {
        "slug": "individual-enchiladas",
        "label": "Individually Packaged Enchiladas (includes chips & salsa)",
        "modifier_groups": [_ENCHILADA_SAUCE_GROUP],
        "items": [
            {"slug": "ind-veggie-enchiladas",          "label": "Veggie Enchiladas (Individually Packaged)",          "reference_price": 13.99},
            {"slug": "ind-beef-fajita-enchiladas",     "label": "Beef Fajita Enchiladas (Individually Packaged)",     "reference_price": 15.49},
            {"slug": "ind-chicken-enchiladas",         "label": "Chicken Enchiladas (Individually Packaged)",         "reference_price": 14.99},
            {"slug": "ind-seafood-enchiladas",         "label": "Seafood Enchiladas (Individually Packaged)",         "reference_price": 14.89},
            {"slug": "ind-cheese-enchiladas",          "label": "Cheese Enchiladas (Individually Packaged)",          "reference_price": 13.49},
            {"slug": "ind-chicken-fajita-enchiladas",  "label": "Chicken Fajita Enchiladas (Individually Packaged)",  "reference_price": 14.99},
            {"slug": "ind-ground-beef-enchiladas",     "label": "Ground Beef Enchiladas (Individually Packaged)",     "reference_price": 14.49},
            {"slug": "ind-tamales",                    "label": "Tamales (Individually Packaged)",                    "reference_price": 13.99},
            {"slug": "ind-pork-enchiladas",            "label": "Pork Enchiladas (Individually Packaged)",            "reference_price": 13.99},
        ],
    },
    {
        "slug": "a-la-carte-meats",
        "label": "A La Carte Meats",
        "modifier_groups": [],
        "items": [
            {"slug": "jumbo-brochette-shrimp",  "label": "Jumbo Brochette Shrimp",      "reference_price": 13.99, "serves": "4 pieces"},
            {"slug": "andouille-sausage",       "label": "Andouille Grilled Sausage",   "reference_price": 10.99, "serves": "4 pieces"},
            {"slug": "baja-baby-back-ribs",     "label": "Baja Baby Back Ribs",         "reference_price": 12.99, "serves": "4 pieces"},
            {"slug": "beef-fajita-by-pound",    "label": "Beef Fajita (by weight)",     "reference_price": 22.00, "serves": "1/3", "item_modifiers": [_ALACARTE_SIZE_GROUP]},
            {"slug": "chicken-fajita-by-pound", "label": "Chicken Fajita (by weight)",  "reference_price": 19.00, "serves": "1/3", "item_modifiers": [_ALACARTE_SIZE_GROUP]},
        ],
    },
    {
        "slug": "sides",
        "label": "Sides",
        "modifier_groups": [_SIDE_SIZE_GROUP],
        "items": [
            {"slug": "queso-chips",      "label": "Queso & Chips",     "reference_price": 14.00, "serves": "10", "most_ordered": True, "description": "Melted cheese dip + tortilla chips."},
            {"slug": "guacamole-chips",  "label": "Guacamole & Chips", "reference_price": 14.00, "serves": "10"},
            {"slug": "rice",             "label": "Rice",              "reference_price": 6.00,  "serves": "10"},
            {"slug": "refried-beans",    "label": "Refried Beans",     "reference_price": 6.00,  "serves": "10"},
            {"slug": "grated-cheese",    "label": "Grated Cheese",     "reference_price": 5.00,  "serves": "10"},
            {"slug": "pickled-jalapenos","label": "Pickled Jalapeños", "reference_price": 5.00,  "serves": "10"},
            {"slug": "charro-beans",     "label": "Charro Beans",      "reference_price": 6.00,  "serves": "10"},
            {"slug": "red-sauce",        "label": "Red Sauce",         "reference_price": 6.00,  "serves": "10", "description": "Homemade salsa."},
            {"slug": "sour-cream",       "label": "Sour Cream",        "reference_price": 5.00,  "serves": "10"},
            {"slug": "fresh-avocado",    "label": "Fresh Avocado",     "reference_price": 13.00, "serves": "10"},
            {"slug": "pico-de-gallo",    "label": "Pico De Gallo",     "reference_price": 7.00,  "serves": "10", "description": "Mild fresh salsa."},
            {"slug": "green-sauce",      "label": "Green Sauce",       "reference_price": 6.00,  "serves": "10", "description": "Homemade salsa."},
            {"slug": "flour-tortillas",  "label": "Flour Tortillas",   "reference_price": 6.00,  "serves": "12", "description": "Homemade."},
            {"slug": "corn-tortillas",   "label": "Corn Tortillas",    "reference_price": 6.00,  "serves": "12"},
            {"slug": "black-beans",      "label": "Black Beans",       "reference_price": 6.00,  "serves": "10"},
            {"slug": "fresh-jalapenos",  "label": "Fresh Jalapeños",   "reference_price": 5.00,  "serves": "10"},
        ],
    },
    {
        "slug": "desserts",
        "label": "Desserts",
        "modifier_groups": [],
        "items": [
            {"slug": "churros",      "label": "Churros",     "reference_price": 4.00, "most_ordered": True,
             "description": "Fried pastry with cinnamon, sugar, caramel-filled, chocolate-syrup topped. 2 pieces per person."},
            {"slug": "sopapillas",   "label": "Sopapillas",  "reference_price": 4.00,
             "description": "Lightly fried flour tortillas with cinnamon + sugar, dipping sauce. 2 pieces per person."},
            {"slug": "tres-leches",  "label": "Tres Leches", "reference_price": 8.00,
             "description": "Sponge cake soaked in 3 milks. 1 slice per person, 3 inches."},
        ],
    },
    {
        "slug": "beverages",
        "label": "Beverages",
        "modifier_groups": [],
        "items": [
            {"slug": "unsweet-tea",      "label": "Gallon Unsweet Tea", "reference_price": 9.99,  "serves": "8",  "most_ordered": True, "description": "House-made."},
            {"slug": "sweet-tea",        "label": "Gallon Sweet Tea",   "reference_price": 10.99, "serves": "8",  "most_ordered": True, "description": "House-made."},
            {"slug": "lemonade",         "label": "Lemonade",           "reference_price": 11.99, "serves": "8",  "most_ordered": True, "description": "Fresh-squeezed."},
            {"slug": "bottled-water",    "label": "Bottled Waters",     "reference_price": 4.00},
            {"slug": "bottled-soda",     "label": "20oz Bottled Sodas", "reference_price": 4.50},
            {"slug": "jarritos",         "label": "12oz Jarritos Sodas","reference_price": 4.00,  "description": "Fruit-flavored Mexican sodas."},
        ],
    },
    {
        "slug": "miscellaneous",
        "label": "Miscellaneous",
        "modifier_groups": [],
        "items": [
            {"slug": "chafing-set", "label": "Complete Catering Chafing Dish Set", "reference_price": 125.00, "serves": "50",
             "description": "Pans, racks, fuel, serving utensils. Keeps food hot up to 2 hours."},
        ],
    },
]


def total_item_count() -> int:
    return sum(len(c["items"]) for c in CATEGORIES)
