from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from app.services.toast_client import ToastClient, restaurant_guids
from app.services.toast_webhook_store import (
    ToastWebhookStore,
    business_dates_for_backfill,
    synthetic_event_guid,
)


log = logging.getLogger("toast_webhook_backfill")


def _load_toast_env_file(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    wanted = {
        "TOAST_ANALYTICS_CLIENT_ID",
        "TOAST_ANALYTICS_CLIENT_SECRET",
        "TOAST_CLIENT_ID",
        "TOAST_CLIENT_SECRET",
        "TOAST_RESTAURANT_GUID_COPPERFIELD",
        "TOAST_RESTAURANT_GUID_TOMBALL",
    }
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].strip()
            name, value = stripped.split("=", 1)
            name = name.strip()
            if name not in wanted:
                continue
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(name, value)


def _dimension_guid(row: dict[str, Any]) -> str | None:
    guid = row.get("guid")
    if isinstance(guid, str) and guid.strip():
        return guid.strip()
    ref = row.get("reference")
    if isinstance(ref, dict) and ref.get("guid"):
        return str(ref["guid"]).strip()
    return None


def _dimension_name(row: dict[str, Any]) -> str | None:
    for key in ("name", "displayName", "description", "jobName"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def sync_dimensions(store: ToastWebhookStore, refresh: bool) -> dict[str, int]:
    client = ToastClient.shared()
    counts: dict[str, int] = {}
    for store_key, restaurant_guid in restaurant_guids().items():
        pulls = {
            "table": lambda: client.fetch_tables(store_key, restaurant_guid, refresh=refresh),
            "employee": lambda: client.fetch_employees(store_key, restaurant_guid, refresh=refresh),
            "job": lambda: client.fetch_jobs(store_key, restaurant_guid, refresh=refresh),
        }
        for domain, pull in pulls.items():
            rows = pull() or []
            written = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                guid = _dimension_guid(row)
                if not guid:
                    continue
                store.upsert_dimension_item(
                    domain=domain,
                    store_key=store_key,
                    toast_guid=guid,
                    name=_dimension_name(row),
                    payload=row,
                    source="api",
                )
                written += 1
            counts[f"{store_key}.{domain}"] = written
    return counts


def backfill_orders(store: ToastWebhookStore, days: int, refresh: bool) -> dict[str, int]:
    client = ToastClient.shared()
    counts: dict[str, int] = {}
    for store_key, restaurant_guid in restaurant_guids().items():
        written = 0
        for business_date in business_dates_for_backfill(days):
            orders = client.fetch_orders_for_date(store_key, restaurant_guid, business_date, refresh=refresh)
            for order in orders or []:
                if not isinstance(order, dict):
                    continue
                event = {
                    "timestamp": order.get("modifiedDate") or order.get("openedDate") or datetime.utcnow().isoformat() + "Z",
                    "eventCategory": "order_updated",
                    "eventType": "order_updated",
                    "guid": synthetic_event_guid(f"ordersBulk:{store_key}:{business_date}", order),
                    "details": {"restaurantGuid": restaurant_guid, "order": order},
                }
                raw = json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
                store.store_webhook_event(
                    payload=event,
                    raw_body=raw,
                    headers={
                        "Toast-Attempt-Number": "0",
                        "Toast-Event-Type": "order_updated",
                        "Toast-Event-Category": "order_updated",
                        "Toast-Restaurant-External-ID": restaurant_guid,
                    },
                    signature_verified=False,
                    source="ordersBulk_backfill",
                )
                written += 1
        counts[store_key] = written
    return counts


def sync_labor_recent(store: ToastWebhookStore, days: int, refresh: bool) -> dict[str, int]:
    client = ToastClient.shared()
    end = datetime.utcnow() - timedelta(hours=5)
    start = end - timedelta(days=max(days - 1, 0))
    counts: dict[str, int] = {}
    for store_key, restaurant_guid in restaurant_guids().items():
        time_entries = client.fetch_time_entries(store_key, restaurant_guid, start, end, refresh=refresh)
        shifts = client.fetch_shifts(store_key, restaurant_guid, start, end, refresh=refresh)
        for domain, rows in (("time_entry", time_entries or []), ("shift", shifts or [])):
            written = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                guid = _dimension_guid(row)
                if not guid:
                    continue
                store.upsert_dimension_item(
                    domain=domain,
                    store_key=store_key,
                    toast_guid=guid,
                    name=_dimension_name(row),
                    payload=row,
                    source="api",
                )
                written += 1
            counts[f"{store_key}.{domain}"] = written
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed and backfill the CK Toast webhook database.")
    parser.add_argument("--db", default=os.getenv("TOAST_WEBHOOK_DB"))
    parser.add_argument("--toast-env-file", default=r"C:\Users\sam\cena-secrets\toast_render_env.txt")
    parser.add_argument("--seed-identities", action="store_true")
    parser.add_argument("--sync-dimensions", action="store_true")
    parser.add_argument("--backfill-orders-days", type=int, default=0)
    parser.add_argument("--sync-labor-days", type=int, default=0)
    parser.add_argument("--materialize-employee-profile-dbs", action="store_true")
    parser.add_argument("--employee-profile-db-dir", default=os.getenv("TOAST_EMPLOYEE_PROFILE_DB_DIR"))
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _load_toast_env_file(args.toast_env_file)
    store = ToastWebhookStore(args.db)
    store.init_schema()

    output: dict[str, Any] = {"ok": True}
    if args.seed_identities:
        output["seed"] = store.seed_employee_profiles_and_identity()
    if args.sync_dimensions:
        output["dimensions"] = sync_dimensions(store, args.refresh)
    if args.sync_labor_days > 0:
        output["labor"] = sync_labor_recent(store, args.sync_labor_days, args.refresh)
    if args.backfill_orders_days > 0:
        output["orders"] = backfill_orders(store, args.backfill_orders_days, args.refresh)
    if args.materialize_employee_profile_dbs:
        output["employee_profile_dbs"] = store.materialize_employee_profile_databases(
            output_dir=args.employee_profile_db_dir
        )
    output["health"] = store.health()
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
