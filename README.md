# Cenas Kitchen Order Processor — Claude edition

Flask webapp that ingests ezCater order PDFs, extracts structured order data via **Claude vision**, applies kitchen prep rules, builds 4 view sheets (Master / Kitchen / Driver / Prep Expo), and exports a print-ready xlsx workbook. Same rules, portions, view layout, and print format as the upstream Cenas Kitchen processor; the only swap is **Gemini → Claude vision** for PDF extraction. Distance lookups still use **Google Maps Distance Matrix** for accuracy.

## Setup

```bash
python -m venv .venv
source .venv/Scripts/activate    # Windows bash; on PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a `.env` file at the repo root:

```
DATABASE_URL=sqlite:///cenas_kitchen.db
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6
GOOGLE_MAPS_API_KEY=AIza...
SECRET_KEY=replace-me
MANAGER_PASSWORD_COPPERFIELD=changeme
MANAGER_PASSWORD_TOMBALL=changeme
CORPORATE_PASSWORD=changeme
LOG_LEVEL=INFO
```

For Postgres in production: `DATABASE_URL=postgresql://user:pwd@host/db`. SQLite is fine for local dev (the `JSON` columns work on both).

Apply the schema:

```bash
alembic upgrade head
```

Add drivers per location (passed to the Flask CLI):

```bash
flask --app wsgi create-driver "Driver Name" copperfield
flask --app wsgi create-driver "Driver Name" tomball
```

Run the dev server:

```bash
python wsgi.py
```

Visit http://localhost:5000/

## Pipeline

1. PDF upload at `/orders` (multi-file, async — POST returns a job id; the page polls `/orders/status/<job>/poll`)
2. PyMuPDF rasterizes each page at 300 DPI; an upscaled crop of the address block is appended as the last image
3. **Claude** (default `claude-sonnet-4-6`, override with `ANTHROPIC_MODEL`) sees all page images plus the address crop and is forced to call the `submit_order_data` tool — its `input_schema` is derived from the `RawOrder` TypedDict via Pydantic, so the response shape stays in sync with the schema
4. `normalize_order` matches each item to `MENU_CATALOG`, parses choices (packaging / beans / tortillas / containers / dressings / sauces / tableware sub-counts)
5. `kitchen_engine` runs per-package rules: `rules_fajitas` (mixed: 2.25 oz chicken + 2.25 oz beef per person; solo: 4.5 oz/pp), `rules_brochette` (2 packs/pp), `rules_veggie` (6 oz/pp), `rules_salads` (cobb + fajita-and-salad with all the lettuce/avocado/tomato/cuke/cheese/bacon/egg/olives portions), `party_sides` (onions/pico/sour cream 1.5 oz [1.0 over 30], guac 1.5 oz, rice 3.8 oz [3.5 over 30], beans 3.8 oz [3.5 over 30], chips 4 oz, red/green sauce 1.5 oz), tortilla packets (2.5/2 packets/pp with proper half/half splitting), and tableware utensil aggregation across all trays + dessert tongs + cobb prep components
6. `dispatch_planner` pairs same-store same-day orders (90 min kitchen-ready buffer, 15 min depart, 20 min multi-stop service buffer), scores all feasible pairs by `(total_late, total_drive)`, greedy-assigns best-first, solo fallback. Drive minutes from Google Maps Distance Matrix.
7. `master_sheet_map` flattens the order + kitchen + dispatch into a Master view; Kitchen / Driver / Prep Expo pull subsets of those keys
8. `grid_builder` produces the in-page tabular UI grids; openpyxl exports xlsx with section headers, freeze panes (B4), repeat title rows (1:3), landscape print, fit-to-1-wide, page break + individual order block per order

## Routes

| Path | What |
|---|---|
| `/` | Home (portal cards) |
| `/orders` | PDF upload + 4 view tabs (Master / Kitchen / Driver / Prep Expo) + Download Excel |
| `/manager` | Manager dashboard (per-location password). Driver CRUD + log delivery (on_time / tracking / picture / five_star → $10 bonus criteria, ex_miles + verified) |
| `/driver` | Driver portal (name + location lookup); view own logs |
| `/review` | Last 40 orders, drill-in for items + warnings |

## What changed vs. the upstream Gemini version

| File | Change |
|---|---|
| `app/infra/pdf_reader.py` | Anthropic SDK `messages.create` with vision + `tool_use` enforced schema (was Gemini `generate_content` with `response_schema`); same prompt verbatim, same retry loop, same address crop |
| `app/config.py` | `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` / `ANTHROPIC_MAX_TOKENS` (was `GEMINI_KEY` / `GEMINI_MODEL`) |
| `app/models.py` + initial migration | Cross-DB `sa.JSON` column type (was Postgres-specific `JSONB`); works on both Postgres and SQLite. Initial migration absorbs the old "fix driver constraints" migration so the schema is correct from rev 1; the second revision is a kept-but-no-op stub for upgrade-path compatibility |
| `requirements.txt` | `anthropic>=0.40.0` instead of `google-genai` / `google-auth`; dropped four unused PDF libs (only PyMuPDF is actually called); dropped unused `playwright` |
| Wording | "Gemini" → "Claude" in `validation.py` warnings and `orders_service.py` failure stage names |

## What did NOT change

- Every rule file (`rules_fajitas`, `rules_brochette`, `rules_veggie`, `rules_salads`, `party_pack_rules`, `rules_utils`)
- Container math (`containers.py`)
- All portion sizes, per-person rates, headcount-30 thresholds, tableware aggregation
- `MENU_CATALOG` (every alias, item_key, package_type, sheet section, sort)
- Every `RowSpec` in `master_sheet_map` (Master / Kitchen / Driver / Prep Expo) — all sections, labels, sort orders preserved
- `grid_builder`, `ticket_context`, `delivery_timing` (route math), `dispatch_planner`
- All HTML templates and the full `style.css` (color palette: #484041 nav, #A44A3F terracotta, #729B79 sage, #F5F5F5 background)
- `export_xlsx.py` (print format: section headers, freeze panes B4, repeat title rows 1:3, landscape, fit-to-1-wide, page breaks per order, individual order blocks)
- All 4 web blueprints (`ezcater_routes`, `manager_routes`, `driver_routes`, `review_routes`)
- Google Maps Distance Matrix client (`app/infra/geo.py`) — kept for distance accuracy
- Postgres / Render compatibility (just also runs on SQLite now)
