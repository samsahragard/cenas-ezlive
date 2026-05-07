# PDF -> extract -> normalize -> kitchen ticket (orchestrator)
from __future__ import annotations

import logging
from typing import Any, List, Dict
import time
import traceback

logger = logging.getLogger(__name__)

from app.infra.pdf_reader import get_pdf_as_images, extract_order_from_pdf
from app.domain.validation import validate_raw_order, validate_normalized_order
from app.domain.menu_catalog import MenuCatalog, MENU_CATALOG
from app.domain.normalize import normalize_order
from app.domain.kitchen_engine import build_kitchen_result
from app.domain.ticket_context import build_ticket_context
from app.domain.master_sheet_map import build_all_outputs
from app.domain.grid_builder import build_all_view_grids
from app.infra.export_xlsx import export_view_grids_to_xlsx
from app.services.dispatch_planner import build_dispatch_plans


catalog = MenuCatalog(MENU_CATALOG)


def _failure_result(pdf_path: str, stage: str, error: Exception, started_at: float) -> Dict[str, Any]:
    return {
        "success": False,
        "pdf_path": pdf_path,
        "stage": stage,
        "error": str(error),
        "processing_seconds": round(time.time() - started_at, 2),
    }


def process_single_pdf(pdf_path: str) -> Dict[str, Any]:
    """
    Full pipeline for one PDF.
    Returns a structured success/failure payload instead of raising.
    """
    started_at = time.time()

    try:
        pdf_images = get_pdf_as_images(pdf_path)
    except Exception as e:
        return _failure_result(pdf_path, "retrieving_pdf", e, started_at)

    try:
        raw_order = extract_order_from_pdf(pdf_images)
    except Exception as e:
        return _failure_result(pdf_path, "claude_extraction", e, started_at)
    
    raw_warnings = validate_raw_order(raw_order)

    try:
        normalized_order = normalize_order(raw_order, catalog)
    except Exception as e:
        return _failure_result(pdf_path, "normalizing_order", e, started_at)
    
    norm_warnings = validate_normalized_order(normalized_order)
    all_warnings = raw_warnings + norm_warnings

    try:
        kitchen_result = build_kitchen_result(normalized_order)
    except Exception as e:
        return _failure_result(pdf_path, "building_result", e, started_at)

    return {
        "success": True,
        "pdf_path": pdf_path,
        "order_id": normalized_order.get("order_id"),
        "raw_order": raw_order,
        "normalized_order": normalized_order,
        "kitchen_result": kitchen_result,
        "processing_seconds": round(time.time() - started_at, 2),
        "warnings": all_warnings,
        "needs_review": bool(all_warnings),
    }


def process_multiple_pdfs(pdf_paths: List[str], collapse_empty_rows: bool = False) -> Dict[str, Any]:
    """
    Process multiple PDFs safely.
    Failed PDFs do not stop successful PDFs from continuing.
    """
    processed_orders: List[Dict[str, Any]] = []

    for pdf_path in pdf_paths:
        result = process_single_pdf(pdf_path)
        processed_orders.append(result)

    successful_orders = [o for o in processed_orders if o.get("success")]
    failed_orders = [o for o in processed_orders if not o.get("success")]

    if not successful_orders:
        return {
            "success": False,
            "orders": processed_orders,
            "grids": None,
            "success_count": 0,
            "failure_count": len(failed_orders),
            "error": "All PDFs failed to process.",
        }

    try:
        normalized_orders = [o["normalized_order"] for o in successful_orders]
        dispatch_plans = build_dispatch_plans(normalized_orders)
    except Exception as e:
        return {
            "success": False,
            "orders": processed_orders,
            "grids": None,
            "success_count": len(successful_orders),
            "failure_count": len(failed_orders),
            "error": f"dispatch_planning_failed: {str(e)}",
        }

    for bundle in successful_orders:
        try:
            order = bundle["normalized_order"]
            kitchen_result = bundle["kitchen_result"]

            dispatch = dispatch_plans.get(order.get("order_id"), {})

            order["route_group_id"] = dispatch.get("route_group_id")
            order["route_stop_index"] = dispatch.get("route_stop_index")
            order["assigned_driver"] = dispatch.get("assigned_driver")

            ctx = build_ticket_context(order, kitchen_result, dispatch)
            views = build_all_outputs(order, kitchen_result, ctx, catalog)

            bundle["ticket_context"] = ctx
            bundle["views"] = views
            bundle["dispatch"] = dispatch

        except Exception as e:
            bundle["success"] = False
            bundle["stage"] = "post_processing"
            bundle["error"] = str(e)
            bundle["traceback"] = traceback.format_exc()
            logger.error("Post-processing failed for order %s: %s", bundle.get("order_id", "?"), e, exc_info=True)

    # Re-split after post-processing, since some successful orders may have failed here
    final_successful_orders = [o for o in processed_orders if o.get("success")]
    final_failed_orders = [o for o in processed_orders if not o.get("success")]

    if not final_successful_orders:
        return {
            "success": False,
            "orders": processed_orders,
            "grids": None,
            "success_count": 0,
            "failure_count": len(final_failed_orders),
            "error": "All PDFs failed before grid generation.",
        }

    final_successful_orders.sort(key=lambda b: (
        b.get("dispatch", {}).get("assigned_driver") or "",
        b.get("dispatch", {}).get("route_stop_index") or 0,
    ))

    try:
        grids = build_all_view_grids(
            final_successful_orders,
            collapse_empty_rows=collapse_empty_rows,
        )
    except Exception as e:
        return {
            "success": False,
            "orders": processed_orders,
            "grids": None,
            "success_count": len(final_successful_orders),
            "failure_count": len(final_failed_orders),
            "error": f"grid_build_failed: {str(e)}",
        }

    return {
        "success": True,
        "orders": processed_orders,
        "grids": grids,
        "success_count": len(final_successful_orders),
        "failure_count": len(final_failed_orders),
    }


def process_and_export(
    pdf_paths: List[str],
    collapse_empty_rows: bool = False,
) -> Dict[str, Any]:
    """
    Process PDFs and return Excel workbook as bytes.
    No disk I/O — safe for ephemeral deployments.
    """
    result = process_multiple_pdfs(
        pdf_paths,
        collapse_empty_rows=collapse_empty_rows,
    )

    if not result.get("grids"):
        return {
            "success": False,
            "orders": result.get("orders", []),
            "grids": None,
            "xlsx_bytes": None,
            "collapse_empty_rows": collapse_empty_rows,
            "success_count": result.get("success_count", 0),
            "failure_count": result.get("failure_count", 0),
            "error": result.get("error", "no_grids_generated"),
        }

    try:
        xlsx_bytes = export_view_grids_to_xlsx(result["grids"])
    except Exception as e:
        return {
            "success": False,
            "orders": result["orders"],
            "grids": result["grids"],
            "xlsx_bytes": None,
            "collapse_empty_rows": collapse_empty_rows,
            "success_count": result.get("success_count", 0),
            "failure_count": result.get("failure_count", 0),
            "error": f"export_failed: {str(e)}",
        }

    return {
        "success": True,
        "orders": result["orders"],
        "grids": result["grids"],
        "xlsx_bytes": xlsx_bytes,
        "collapse_empty_rows": collapse_empty_rows,
        "success_count": result.get("success_count", 0),
        "failure_count": result.get("failure_count", 0),
    }
