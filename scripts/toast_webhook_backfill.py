from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.toast_client import ToastClient, restaurant_guids
from app.services.toast_webhook_store import (
    ToastWebhookStore,
    business_dates_for_backfill,
    synthetic_event_guid,
)


log = logging.getLogger("toast_webhook_backfill")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _stable_order_event_payload(store_key: str, business_date: str, order: dict[str, Any]) -> dict[str, str]:
    order_guid = str(order.get("guid") or "").strip()
    if not order_guid:
        order_guid = synthetic_event_guid("ordersBulk:orderPayload", order)
    change_marker = str(
        order.get("modifiedDate")
        or order.get("closedDate")
        or order.get("openedDate")
        or business_date
    ).strip()
    return {
        "store_key": store_key,
        "business_date": business_date,
        "order_guid": order_guid,
        "change_marker": change_marker,
    }


def sync_dimensions(store: ToastWebhookStore, refresh: bool) -> dict[str, int]:
    client = ToastClient.shared()
    counts: dict[str, int] = {}
    for store_key, restaurant_guid in restaurant_guids().items():
        pulls = {
            "table": lambda: client.fetch_tables(store_key, restaurant_guid, refresh=refresh),
            "service_area": lambda: client.fetch_service_areas(store_key, restaurant_guid, refresh=refresh),
            "employee": lambda: client.fetch_employees(store_key, restaurant_guid, refresh=refresh),
            "job": lambda: client.fetch_jobs(store_key, restaurant_guid, refresh=refresh),
        }
        for domain, pull in pulls.items():
            started_at = _utc_now()
            written = 0
            try:
                rows = pull() or []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if store.upsert_api_entity(
                        domain=domain,
                        store_key=store_key,
                        payload=row,
                        source="api_sync",
                    ):
                        written += 1
                store.set_watermark(
                    domain=domain,
                    store_key=store_key,
                    key="last_success_at",
                    value=_utc_now(),
                )
                store.record_pull_log(
                    domain=domain,
                    store_key=store_key,
                    scope_start=None,
                    scope_end=None,
                    started_at=started_at,
                    ok=True,
                    row_count=written,
                )
            except Exception as ex:  # noqa: BLE001 - keep other stores/domains moving.
                log.exception("dimension sync failed for %s/%s", store_key, domain)
                store.record_pull_log(
                    domain=domain,
                    store_key=store_key,
                    scope_start=None,
                    scope_end=None,
                    started_at=started_at,
                    ok=False,
                    row_count=written,
                    error=f"{type(ex).__name__}: {ex}",
                )
            counts[f"{store_key}.{domain}"] = written
    return counts


def backfill_orders(store: ToastWebhookStore, days: int, refresh: bool) -> dict[str, int]:
    client = ToastClient.shared()
    counts: dict[str, int] = {}
    for store_key, restaurant_guid in restaurant_guids().items():
        written = 0
        started_at = _utc_now()
        dates = business_dates_for_backfill(days)
        try:
            for business_date in dates:
                orders = client.fetch_orders_for_date(store_key, restaurant_guid, business_date, refresh=refresh)
                for order in orders or []:
                    if not isinstance(order, dict):
                        continue
                    event = {
                        "timestamp": order.get("modifiedDate") or order.get("openedDate") or _utc_now(),
                        "eventCategory": "order_updated",
                        "eventType": "order_updated",
                        "guid": synthetic_event_guid(
                            f"ordersBulk:{store_key}:{business_date}",
                            _stable_order_event_payload(store_key, business_date, order),
                        ),
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
            store.set_watermark(
                domain="order",
                store_key=store_key,
                key="last_business_date",
                value=(dates[-1] if dates else None),
            )
            store.set_watermark(
                domain="order",
                store_key=store_key,
                key="last_success_at",
                value=_utc_now(),
            )
            store.record_pull_log(
                domain="order",
                store_key=store_key,
                scope_start=(dates[0] if dates else None),
                scope_end=(dates[-1] if dates else None),
                started_at=started_at,
                ok=True,
                row_count=written,
            )
        except Exception as ex:  # noqa: BLE001 - keep other stores moving.
            log.exception("order backfill failed for %s", store_key)
            store.record_pull_log(
                domain="order",
                store_key=store_key,
                scope_start=(dates[0] if dates else None),
                scope_end=(dates[-1] if dates else None),
                started_at=started_at,
                ok=False,
                row_count=written,
                error=f"{type(ex).__name__}: {ex}",
            )
        counts[store_key] = written
    return counts


def sync_labor_recent(store: ToastWebhookStore, days: int, refresh: bool) -> dict[str, int]:
    client = ToastClient.shared()
    end = datetime.now(timezone.utc) - timedelta(hours=5)
    start = end - timedelta(days=max(days - 1, 0))
    counts: dict[str, int] = {}
    for store_key, restaurant_guid in restaurant_guids().items():
        for domain, pull in (
            ("time_entry", lambda: client.fetch_time_entries(store_key, restaurant_guid, start, end, refresh=refresh)),
            ("shift", lambda: client.fetch_shifts(store_key, restaurant_guid, start, end, refresh=refresh)),
        ):
            started_at = _utc_now()
            written = 0
            try:
                rows = pull() or []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if store.upsert_api_entity(
                        domain=domain,
                        store_key=store_key,
                        payload=row,
                        source="api_sync",
                    ):
                        written += 1
                store.set_watermark(
                    domain=domain,
                    store_key=store_key,
                    key="last_success_at",
                    value=_utc_now(),
                )
                store.set_watermark(
                    domain=domain,
                    store_key=store_key,
                    key="last_scope_end",
                    value=end.date().isoformat(),
                )
                store.record_pull_log(
                    domain=domain,
                    store_key=store_key,
                    scope_start=start.date().isoformat(),
                    scope_end=end.date().isoformat(),
                    started_at=started_at,
                    ok=True,
                    row_count=written,
                )
            except Exception as ex:  # noqa: BLE001
                log.exception("labor sync failed for %s/%s", store_key, domain)
                store.record_pull_log(
                    domain=domain,
                    store_key=store_key,
                    scope_start=start.date().isoformat(),
                    scope_end=end.date().isoformat(),
                    started_at=started_at,
                    ok=False,
                    row_count=written,
                    error=f"{type(ex).__name__}: {ex}",
                )
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
