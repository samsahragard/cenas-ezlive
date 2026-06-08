# Company

This file is read-only background for the Cenas Kitchen in-app assistant.
Facts marked TODO are unverified and must not be treated as true.

## Business

- Cenas Kitchen is a Tex-Mex restaurant business with two Houston-area physical stores.
- The operations platform runs at `app.cenaskitchen.com`.
- The platform supports restaurant operations and integrations, including Toast POS data, ezCater catering intake, vendor channels, and scheduling.
- Confirmed legal name: `Cenas Kitchen, LLC`.

## Locations And App Contexts

- Physical stores:
  - DOS MAS / Tomball, URL slug `dos`.
  - UNO MAS / Copperfield, URL slug `uno`.
- All-location app contexts:
  - `corporate`: corporate/all-location operating context.
  - `partner`: owner-only all-location context.
- Corporate order context exists and uses `Corporate Office` as the corporate synthetic customer name.
- TODO (Sam to confirm): corporate warehouse address, operating name, and how staff refer to it in everyday work. The repo confirms corporate app/order context, but the warehouse details are not verified in repo/config.

## Entity Notes

- The app has a legal-company-structure model for entity type, legal name, DBA, formation state, registered agent, registered office, principal office, ownership, and EIN.
- Those legal-structure values are stored as data rows, not hardcoded repo facts. Do not invent them.
- Cenas Fajitas appears as an ezCater/in-house catering menu or storefront name.
- TODO (Sam to confirm): whether Cenas Fajitas is a DBA, a storefront name, a catering listing name, or all of these.

## Sources

- `CENA.md:15-18`
- `app/templates/sam_docs/start.html:5-6`
- `app/templates/privacy.html:41-42`
- `app/templates/privacy.html:157-160`
- `app/models.py:1284-1308`
- `app/web/store_routes.py:1-8`
- `app/web/store_routes.py:62-82`
- `app/services/corporate_shop.py:1-13`
- `app/services/corporate_shop.py:40-44`
- `data/ezcater/menu_prices.json:2`
