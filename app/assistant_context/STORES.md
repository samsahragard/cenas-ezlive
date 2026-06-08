# Stores

Use the exact slugs below when interpreting store-specific assistant questions.
Never expose secret values or raw external identifier values.

## Tomball

- Display name: Tomball.
- Brand/storefront name: DOS MAS.
- URL slug: `dos`.
- Canonical location key: `tomball`.
- Primary origin store id: `store_2`.
- CK prefix: `2`.
- Kitchen wording: CK #2, Tomball kitchen, DOS MAS.
- Operational address from config/location data: 27727 Tomball Pkwy, Tomball, TX 77375.

## Copperfield

- Display name: Copperfield.
- Brand/storefront name: UNO MAS.
- URL slug: `uno`.
- Canonical location key: `copperfield`.
- Primary origin store id: `store_1`.
- CK prefix: `1`.
- Kitchen wording: CK #1, Copperfield kitchen, UNO MAS.
- Operational address from config/location data: 15650 FM 529, Houston, TX 77095.

## All-Location Contexts

- `corporate`: all-location corporate context.
- `partner`: owner-only all-location context.
- `both`: store scope meaning Tomball and Copperfield.

## ezCater Storefronts And Physical Kitchens

- `store_1`: Copperfield physical kitchen.
- `store_2`: Tomball physical kitchen.
- `store_3`: Westheimer ezCater storefront, collapsed to Copperfield physical kitchen.
- `store_4`: Spring Stuebner ezCater storefront, collapsed to Tomball physical kitchen.
- When answering operations questions, prefer physical kitchen wording unless the user explicitly asks about ezCater storefront IDs.

## Alias Map

- `dos`, `dos mas`, and `tomball` mean Tomball.
- `uno`, `uno mas`, and `copperfield` mean Copperfield.
- `CK #1` means Copperfield / UNO MAS.
- `CK #2` means Tomball / DOS MAS.

## Restaurant Identifier Names

Identifier names only; do not print values.

- Toast restaurant GUID env vars: `TOAST_RESTAURANT_GUID_COPPERFIELD`, `TOAST_RESTAURANT_GUID_TOMBALL`.
- Toast schedule location env var: `TOAST_SCHEDULE_LOCATIONS`.
- ezCater caterer UUID map constant: `CATERER_UUID_TO_STORE`.
- Sling organization id constant: `DEFAULT_ORG_ID`.

## TODOs

- TODO (Sam to confirm): privacy contact zip differs from the operational Tomball zip in config/location data.
- TODO (Sam to confirm): corporate warehouse/store-room address and whether staff call it corporate, warehouse, office, or another term.

## Sources

- `app/web/store_routes.py:1-8`
- `app/web/store_routes.py:62-82`
- `CENA.md:15-18`
- `app/config.py:13-15`
- `data/produce/locations.json:1-11`
- `app/domain/normalize.py:15-56`
- `app/services/driver_assignment_jobs.py:26-37`
- `app/web/assistant_routes.py:949-956`
- `app/services/sling_client.py`
- `app/services/toast_client.py`
- `app/services/toast_reports.py`
- `app/web/ezcater_webhook.py`
