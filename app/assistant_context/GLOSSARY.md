# Glossary

Use these terms to interpret Cenas-specific questions. Do not treat TODOs as facts.

## Store Terms

- `dos`: URL slug for Tomball / DOS MAS.
- `uno`: URL slug for Copperfield / UNO MAS.
- `tomball`: canonical location key for Tomball.
- `copperfield`: canonical location key for Copperfield.
- `corporate`: all-location corporate context.
- `partner`: owner-only all-location context.
- `both`: store scope covering Tomball and Copperfield.
- `store_scope`: a user's allowed store scope, commonly `tomball`, `copperfield`, `both`, or `none` depending on model.
- CK #1: Copperfield / UNO MAS kitchen.
- CK #2: Tomball / DOS MAS kitchen.

## Order And Catering Terms

- `caterings`: Cenas shorthand for catering orders, usually the ezCater/order pipeline unless the user explicitly says in-house catering.
- `orders.store_summary`: sanitized aggregate assistant tool area for order/catering counts such as today/upcoming totals, needs-driver counts, status counts, and store split.
- `store_1`: Copperfield physical kitchen.
- `store_2`: Tomball physical kitchen.
- `store_3`: Westheimer ezCater storefront collapsed to Copperfield kitchen.
- `store_4`: Spring Stuebner ezCater storefront collapsed to Tomball kitchen.
- `ghost storefront`: an ezCater storefront/listing that is not the physical kitchen used for prep.
- `pickup kitchen`: the physical kitchen where an order is prepped and picked up after ghost-storefront collapse.
- `needs driver`: catering order state or aggregate meaning an order still needs a suitable driver assignment.
- `tracking missing`: order state or aggregate meaning delivery tracking is not available.
- `In-House Catering`: staff-built quote/order flow based on the Cenas Fajitas menu.
- `Cenas Fajitas`: ezCater/in-house catering menu or storefront name in repo context.
- TODO (Sam to confirm): `covers` usually means guest/person count, but confirm whether Cenas wants the assistant to map it to Toast guest count, catering headcount, table covers, or another metric.

## People And Role Terms

- `GM`: General Manager; store-scoped management role.
- `KM`: Kitchen Manager; store-scoped management role.
- `Assistant KM`: Assistant Kitchen Manager.
- `FOH Manager`: front-of-house manager.
- `Corporate Chef`: multi-store chef role.
- `Prep Manager`: multi-store prep/produce role.
- `Expo`: management-section role with orders/KDS access.
- `corporate_driver`: in-house corporate driver role.
- `driver`: ezCater/driver-flow role; route-scoped to own bids/history in the permission map.
- `BOH`: back of house.
- `FOH`: front of house.
- `KDS`: kitchen display system.

## Systems

- Toast: POS and reporting system. Use env var names only when referring to restaurant GUIDs.
- ezCater: catering intake and driver assignment pipeline.
- Sling: scheduling source being replaced by the internal scheduling system.
- Vendor channels: non-Toast vendor/order integrations used by the operations platform.
- Render: hosting platform for the Flask app.
- Mini_IT13: internal host used by assistant/runtime and operational services.

## Assistant Behavior Notes

- Prefer Cenas words over generic words when context is clear: Tomball, Copperfield, DOS MAS, UNO MAS, CK #1, or CK #2.
- Use this file to interpret terms, not to answer private data questions by itself.
- For actual counts or records, tool output is the source of truth.
- Do not expose secrets, raw external IDs, tokens, passcodes, customer PII, employee contact details, or private contact fields.

## Sources

- `app/web/store_routes.py:1-8`
- `app/web/store_routes.py:62-82`
- `app/web/assistant_routes.py:949-956`
- `app/domain/normalize.py:15-56`
- `docs/cenas_ai_tool_inventory.md:29-36`
- `docs/cenas_ai_tool_inventory.md:341-360`
- `app/services/permission_catalog.py:26-48`
- `app/data/in_house_catering_menu.py:1-5`
- `data/ezcater/menu_prices.json:2`
