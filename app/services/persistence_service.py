from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from app.db import get_db
from app.models import Order, OrderItem, PrepBreakdownRecord, ProcessingJob, ProcessingOrder, FailureSnapshot

def persist_processing_job(pdf_count: int) -> int:
    db = next(get_db())
    try:
        job = ProcessingJob(
            status="processing",
            pdf_count=pdf_count,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def persist_results(job_db_id: int, bundles: list[dict[str, Any]]) -> None:
    db = next(get_db())
    try:
        success_count = 0
        failure_count = 0

        for bundle in bundles:
            if bundle.get("success"):
                order_db_id = _persist_order(db, bundle)
                _persist_processing_order(db, job_db_id, bundle, order_db_id, status="success")
                success_count += 1
            else:
                proc_order = _persist_processing_order(db, job_db_id, bundle, None, status="failed")
                db.flush()
                db.add(FailureSnapshot(
                    processing_order_id=proc_order.id,
                    raw_order_json=bundle.get("raw_order"),
                    normalized_order_json=bundle.get("normalized_order"),
                    traceback_text=bundle.get("traceback"),
                ))
                failure_count += 1
        job = db.query(ProcessingJob).filter_by(id=job_db_id).first()
        if job:
            job.status = "done"
            job.success_count = success_count
            job.failure_count = failure_count
            job.completed_at = datetime.utcnow()

        # Listing pages (/orders/tomball, /orders/copperfield) browse the full
        # history, so we no longer cap the orders table.
        db.commit()

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def _persist_order(db, bundle: dict[str, Any]) -> int:
    normalized = bundle["normalized_order"]
    kitchen_result = bundle["kitchen_result"]
    dispatch = bundle.get("dispatch") or {}
    warnings = bundle.get("warnings", [])
    external_id = normalized.get("order_id")
    pdf_path = bundle.get("pdf_path", "")

    # overwrite if exists
    existing = db.query(Order).filter_by(external_order_id=external_id).first()
    if existing:
        db.delete(existing)
        db.flush()

    order = Order(
        external_order_id=external_id,
        external_delivery_id=bundle.get("external_delivery_id"),
        source_filename=os.path.basename(pdf_path) if pdf_path else None,
        client=normalized.get("client"),
        upon_delivery_ask_for=normalized.get("upon_delivery_ask_for"),
        customer_phone=normalized.get("customer_phone"),
        delivery_address=normalized.get("delivery_address"),
        delivery_instructions=normalized.get("delivery_instructions"),
        headcount=normalized.get("headcount"),
        reported_store=normalized.get("reported_store"),
        reported_store_id=normalized.get("reported_store_id"),
        origin_store_id=normalized.get("origin_store_id"),
        delivery_date=normalized.get("date"),
        deliver_at=normalized.get("deliver_at"),
        delivery_window=normalized.get("delivery_window"),
        setup_required=normalized.get("setup_required"),
        flags=normalized.get("flags"),
        needs_review=bundle.get("needs_review", False),
        warning_count=len(warnings),
        # Cenas's driver-bid operation is independent of ezCater's
        # courier assignment (per Sam #1646): every order should enter
        # the bid pool the moment ingest completes, since 'ingest done'
        # is just a job-state on our side and has no bearing on whether
        # a Cenas driver should be allowed to request it. Default to
        # 'available' (the requestable lifecycle state); the prior
        # 'processed' marker was a job-completion artifact predating
        # the bid system and is decommissioned per samai #1645.
        status="available",
        kitchen_ready_time=dispatch.get("kitchen_ready_time"),
        driver_departure_time=dispatch.get("driver_departure_time"),
        assigned_driver=dispatch.get("assigned_driver"),
        route_group_id=dispatch.get("route_group_id"),
        route_stop_index=dispatch.get("route_stop_index"),
    )
    db.add(order)
    db.flush()

    breakdowns = kitchen_result.get("breakdowns", [])

    for idx, item in enumerate(normalized.get("normalized_items", [])):
        order_item = OrderItem(
            order_id=order.id,
            raw_alias=item.get("source", {}).get("raw_alias", ""),
            item_key=item.get("item_key"),
            qty=item.get("qty"),
            package_type=item.get("package_type"),
            packaging=item.get("choices", {}).get("packaging"),
            choices=item.get("choices"),
            extras=item.get("extras"),
            flags=item.get("flags"),
            source=item.get("source"),
        )
        db.add(order_item)
        db.flush()

        if idx < len(breakdowns):
            db.add(PrepBreakdownRecord(
                order_item_id=order_item.id,
                breakdown=breakdowns[idx],
            ))

    # Compute order revenue from item unit prices baked into raw_alias
    # ("Item @ $XX.XX"). Used by /reports/sales (ezCater channel) and the
    # labor cost ratio. Falls back to the scraped storefront menu when an
    # item line has no $ token.
    try:
        from app.services.ezcater_pricing import compute_order_total
        items = (db.query(OrderItem)
                 .filter(OrderItem.order_id == order.id)
                 .all())
        order.total_amount = compute_order_total(items)
    except Exception:
        # Non-fatal — total can be backfilled later
        import logging
        logging.getLogger(__name__).exception("compute_order_total failed for order %s", order.id)


def _trim_order_table(db, limit: int = 40) -> None:
    total = db.query(Order).count()
    if total <= limit:
        return
    excess = total - limit
    oldest_ids = (
        db.query(Order.id)
        .order_by(Order.created_at.asc())
        .limit(excess)
        .all()
    )
    ids_to_delete = [row[0] for row in oldest_ids]
    db.query(Order).filter(Order.id.in_(ids_to_delete)).delete(synchronize_session="fetch")


def _persist_processing_order(
        db, job_db_id: int, bundle: dict[str, Any], order_db_id: int | None, status: str
) -> ProcessingOrder:
    proc_order = ProcessingOrder(
        processing_job_id=job_db_id,
        order_id=order_db_id,
        source_filename=os.path.basename(bundle.get("pdf_path", "") or ""),
        external_order_id=(bundle.get("normalized_order") or {}).get("order_id"),
        status=status,
        stage_failed=bundle.get("stage") if status == "failed" else None,
        error_message=bundle.get("error") if status == "failed" else None,
        warning_count=len(bundle.get("warnings", [])),
        needs_review=bundle.get("needs_review", False),
        processing_seconds=int(bundle.get("processing_seconds", 0) or 0),
        completed_at=datetime.utcnow(),
    )
    db.add(proc_order)
    db.flush()
    return proc_order