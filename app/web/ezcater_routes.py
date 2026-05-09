# API endpoints for upload/extract/breakdown
from __future__ import annotations

import io
import logging
from pathlib import Path
from uuid import uuid4
import threading
import time

from flask import Blueprint, current_app, render_template, request, send_file, jsonify, redirect, url_for, g
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

import os

from app.services.orders_service import process_and_export, process_single_pdf
from app.services.persistence_service import persist_processing_job, persist_results

cater = Blueprint("ezcater", __name__)

_jobs: dict[str, dict] = {}
_JOB_TTL_SECONDS = 3600

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _run_job(app, job_id: str, pdf_paths: list[str], collapse_empty_rows: bool):
    with app.app_context():
        job_db_id = None
        try:
            job_db_id = persist_processing_job(len(pdf_paths))
            result = process_and_export(pdf_paths, collapse_empty_rows=collapse_empty_rows)
            persist_results(job_db_id, result.get("orders", []))
            _jobs[job_id].update({"status": "done", "pdf_count": len(pdf_paths), "result": result})
        except Exception as e:
            _jobs[job_id].update({"status": "failed", "pdf_count": len(pdf_paths), "result": None, "error": str(e)})
        finally:
            _cleanup_files(pdf_paths)


MAX_UPLOAD_BYTES = 30 * 1024 * 1024  # 30 MB


def _save_uploaded_pdfs(files) -> list[str]:
    upload_dir = Path(current_app.root_path).parent / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[str] = []

    for f in files:
        if not f or not f.filename:
            continue

        filename = secure_filename(f.filename)
        if not filename.lower().endswith(".pdf"):
            continue

        unique_name = f"{uuid4().hex}_{filename}"
        path = upload_dir / unique_name
        f.save(path)

        file_size = path.stat().st_size
        if file_size > MAX_UPLOAD_BYTES:
            path.unlink()
            logger.warning("Rejected oversized file: %s (%.1f MB)", filename, file_size / 1024 / 1024)
            continue

        saved_paths.append(str(path))

    return saved_paths


def _cleanup_files(paths: list[str]) -> None:
    for path_str in paths:
        try:
            path = Path(path_str)
            if path.exists():
                path.unlink()
        except Exception as e:
            logger.warning("File cleanup failed for %s: %s", path_str, e)

def _evict_stale_jobs():
    cutoff = time.time() - _JOB_TTL_SECONDS
    stale = [jid for jid, j in list(_jobs.items()) if j.get("created_at", 0) < cutoff]
    for jid in stale:
        del _jobs[jid]


def home():
    """Manager dashboard. Pulls today's deliveries + attention items live
    from the DB so a manager opening the site sees the agenda first, not
    just navigation.

    Note: the route registration for this view moved out — `/` now serves
    the store picker. The `/<store>/` URL prefix layer (store_routes.py)
    calls this function directly after setting g.current_location."""
    from datetime import datetime
    from pathlib import Path
    import json
    from app.db import get_db
    from app.models import Order

    today_iso = datetime.now().strftime("%Y-%m-%d")
    # Per-store filtering: Tomball = stores 2/4, Copperfield = stores 1/3
    location = getattr(g, "current_location", "both")
    tomball_stores = ("store_2", "store_4")
    copperfield_stores = ("store_1", "store_3")

    db = next(get_db())
    try:
        today_q = (
            db.query(Order)
            .filter(Order.delivery_date == today_iso)
            .filter(Order.status != "cancelled")
        )
        review_q = (
            db.query(Order)
            .filter(Order.delivery_date >= today_iso)
            .filter(Order.status != "cancelled")
            .filter(Order.needs_review.is_(True))
        )
        if location == "tomball":
            today_q = today_q.filter(Order.origin_store_id.in_(tomball_stores))
            review_q = review_q.filter(Order.origin_store_id.in_(tomball_stores))
        elif location == "copperfield":
            today_q = today_q.filter(Order.origin_store_id.in_(copperfield_stores))
            review_q = review_q.filter(Order.origin_store_id.in_(copperfield_stores))

        today_orders = today_q.order_by(Order.deliver_at).all()
        review_orders = review_q.order_by(Order.delivery_date, Order.deliver_at).all()

        # KPI counts
        tomball_today = sum(1 for o in today_orders if (o.origin_store_id or "") in ("store_2", "store_4"))
        copperfield_today = len(today_orders) - tomball_today
        heads_today = sum((o.headcount or 0) for o in today_orders)

        # Annotate each today order with location label + a status badge.
        decorated_today = []
        for o in today_orders:
            origin = o.origin_store_id or ""
            loc = "Tomball" if origin in ("store_2", "store_4") else "Copperfield"
            if o.needs_review:
                badge_class, badge_text = "badge-warn", "Needs review"
            elif not (o.client and o.client.strip()):
                badge_class, badge_text = "badge-warn", "No customer"
            elif o.assigned_driver:
                badge_class, badge_text = "badge-good", "On track"
            else:
                badge_class, badge_text = "badge-info", "Unassigned"
            sub_bits = []
            if o.assigned_driver:
                sub_bits.append(f"Driver: {o.assigned_driver}")
            if o.headcount:
                sub_bits.append(f"{o.headcount} heads")
            if o.setup_required:
                sub_bits.append("Setup required")
            decorated_today.append({
                "order_id": o.external_order_id,
                "time": o.deliver_at or "—",
                "name": (o.client or "").strip() or f"{loc} delivery",
                "sub": " · ".join(sub_bits),
                "location": loc,
                "badge_class": badge_class,
                "badge_text": badge_text,
            })

        # Attention list: needs-review first, then orders today with no client.
        attention = []
        for o in review_orders[:3]:
            origin = o.origin_store_id or ""
            loc = "Tomball" if origin in ("store_2", "store_4") else "Copperfield"
            attention.append({
                "kind": "warn",
                "text": f"{o.external_order_id} flagged for review",
                "meta": f"{loc} · {o.delivery_date} {o.deliver_at or ''} · open the order page",
            })
        # Today's orders missing a customer name
        for o in today_orders:
            if not (o.client and o.client.strip()):
                origin = o.origin_store_id or ""
                loc = "Tomball" if origin in ("store_2", "store_4") else "Copperfield"
                attention.append({
                    "kind": "warn",
                    "text": f"{o.external_order_id} missing customer name",
                    "meta": f"{loc} · {o.deliver_at or 'time TBD'} · review before kitchen prep",
                })
                if len(attention) >= 5:
                    break

    finally:
        db.close()

    # Produce winners + last-refresh
    produce_state_dir = Path(os.getenv("PRODUCE_STATE_DIR")
                             or (Path(__file__).resolve().parents[2] / "instance" / "produce"))
    alvarado = {}
    jluna = {}
    try:
        af = produce_state_dir / "alvarado.json"
        if af.exists():
            alvarado = json.loads(af.read_text(encoding="utf-8"))
        jf = produce_state_dir / "jluna.json"
        if jf.exists():
            jluna = json.loads(jf.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("could not read produce state for dashboard")
    produce_winners = len({(it.get("canonical_name"), it.get("canonical_size"))
                           for it in (alvarado.get("items") or []) + (jluna.get("items") or [])
                           if it.get("canonical_name")})
    last_parsed = max(filter(None, [alvarado.get("parsed_at"), jluna.get("parsed_at")]),
                      default=None)
    last_parsed_short = ""
    if last_parsed:
        try:
            dt = datetime.fromisoformat(last_parsed.replace("Z", "+00:00"))
            last_parsed_short = dt.strftime("%b %d, %I:%M %p").replace(" 0", " ").lstrip("0")
        except Exception:
            last_parsed_short = last_parsed[:16]

    # Stale-produce attention item
    if last_parsed:
        try:
            from datetime import timezone
            dt = datetime.fromisoformat(last_parsed.replace("Z", "+00:00"))
            now_utc = datetime.now(timezone.utc)
            age_days = (now_utc - dt).days
            if age_days >= 5:
                attention.append({
                    "kind": "info",
                    "text": f"Produce prices are {age_days} days old",
                    "meta": "Vendor email overdue — site shows stale data",
                })
        except Exception:
            pass

    _now = datetime.now()
    today_long = _now.strftime("%A, %B %d").replace(" 0", " ")

    return render_template(
        "home.html",
        today_iso=today_iso,
        today_long=today_long,
        today_orders=decorated_today,
        attention=attention[:5],
        tomball_today=tomball_today,
        copperfield_today=copperfield_today,
        heads_today=heads_today,
        review_count=len(review_orders),
        produce_winners=produce_winners,
        produce_last_refresh=last_parsed_short,
    )


@cater.route("/orders", methods=["GET", "POST"])
def orders():
    if request.method == "GET":
        return render_template(
            "orders.html",
            grids=None,
            orders=[],
            active_view="master",
            collapse_empty_rows=True,
            error=None,
            success_count=0,
            failure_count=0,
            xlsx_job_id=None,
        )

    files = request.files.getlist("pdfs")
    if not files:
        return render_template(
            "orders.html",
            grids=None,
            orders=[],
            active_view="master",
            collapse_empty_rows=request.form.get("collapse_empty_rows") == "1",
            error="No files were uploaded.",
            success_count=0,
            failure_count=0,
            xlsx_job_id=None,
        )

    pdf_paths = _save_uploaded_pdfs(files)
    job_id = uuid4().hex
    collapse_empty_rows = request.form.get("collapse_empty_rows") == "1"
    _evict_stale_jobs()
    _jobs[job_id] = {"status": "processing", "pdf_count": len(pdf_paths), "result": None, "error": None, "created_at": time.time()}

    t = threading.Thread(
        target=_run_job,
        args=(current_app._get_current_object(), job_id, pdf_paths, collapse_empty_rows),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@cater.route("/download/job/<job_id>")
def download_job(job_id):
    job = _jobs.get(job_id)
    if not job or job["status"] != "done":
        return "Job not found or not complete", 404
    result = job.get("result") or {}
    xlsx_bytes = result.get("xlsx_bytes")
    if not xlsx_bytes:
        return "No export available for this job", 404
    collapse = result.get("collapse_empty_rows", False)
    filename = "ezcater_orders_collapsed.xlsx" if collapse else "ezcater_orders.xlsx"
    return send_file(
        io.BytesIO(xlsx_bytes),
        as_attachment=True,
        download_name=filename,
        mimetype=XLSX_MIME,
    )


@cater.route("/export", methods=["POST"])
def export_orders():
    pdf_paths: list[str] = []

    try:
        files = request.files.getlist("pdfs")
        if not files:
            return render_template(
                "orders.html",
                grids=None,
                orders=[],
                active_view="master",
                collapse_empty_rows=False,
                error="No files were uploaded for export.",
                success_count=0,
                failure_count=0,
                xlsx_job_id=None,
            )

        pdf_paths = _save_uploaded_pdfs(files)
        if not pdf_paths:
            return render_template(
                "orders.html",
                grids=None,
                orders=[],
                active_view="master",
                collapse_empty_rows=False,
                error="No valid PDF files were uploaded for export.",
                success_count=0,
                failure_count=0,
                xlsx_job_id=None,
            )

        collapse_empty_rows = request.form.get("collapse_empty_rows") == "1"
        result = process_and_export(pdf_paths, collapse_empty_rows=collapse_empty_rows)

        if not result.get("success") or not result.get("xlsx_bytes"):
            return render_template(
                "orders.html",
                grids=result.get("grids"),
                orders=result.get("orders", []),
                active_view="master",
                collapse_empty_rows=collapse_empty_rows,
                error=result.get("error", "Export failed."),
                success_count=result.get("success_count", 0),
                failure_count=result.get("failure_count", 0),
                xlsx_job_id=None,
            )

        filename = "ezcater_orders_collapsed.xlsx" if collapse_empty_rows else "ezcater_orders.xlsx"
        return send_file(
            io.BytesIO(result["xlsx_bytes"]),
            as_attachment=True,
            download_name=filename,
            mimetype=XLSX_MIME,
        )

    except Exception as e:
        logger.error("Export route failed: %s", e, exc_info=True)
        return render_template(
            "orders.html",
            grids=None,
            orders=[],
            active_view="master",
            collapse_empty_rows=request.form.get("collapse_empty_rows") == "1",
            error=f"Unexpected export error: {str(e)}",
            success_count=0,
            failure_count=0,
            xlsx_job_id=None,
        )
    finally:
        _cleanup_files(pdf_paths)


@cater.route("/orders/status/<job_id>/poll")
def poll_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    payload = {"status": job["status"]}
    if job["status"] == "done":
        result = job["result"]
        payload["success_count"] = result.get("success_count", 0)
        payload["failure_count"] = result.get("failure_count", 0)
    elif job["status"] == "failed":
        payload["error"] = job.get("error", "Unknown error")
    return jsonify(payload)


@cater.route("/orders/ingest_structured", methods=["POST"])
def ingest_order_structured():
    """Auto-ingest endpoint that takes a structured RawOrder JSON instead
    of a PDF. Used by the ezCater Partner API helper on AiCk — no Claude
    vision step, the API already returns clean structured data.

    Auth: Bearer token in Authorization header, matching INGEST_TOKEN env.
    Body: JSON in the RawOrder shape (see app/domain/schemas.py:RawOrder).
    """
    import time as _time
    started = _time.time()

    expected = os.getenv("INGEST_TOKEN")
    if not expected:
        return jsonify({"error": "INGEST_TOKEN not configured on server"}), 500
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != expected:
        return jsonify({"error": "unauthorized"}), 401

    raw_order = request.get_json(silent=True)
    if not isinstance(raw_order, dict):
        return jsonify({"error": "expected JSON RawOrder body"}), 400

    # Required-field gate (mirrors what the Claude path enforces in pdf_reader).
    # Use truthy check for string fields (empty == missing) but presence-check
    # for headcount (0 or null is valid — some ezCater orders genuinely have
    # no headcount, e.g. small drop-offs).
    required_truthy = ("order_id", "store", "date", "deliver_at", "delivery_address")
    missing = [k for k in required_truthy if not raw_order.get(k)]
    if "headcount" not in raw_order:
        missing.append("headcount")
    if missing:
        return jsonify({"error": f"missing required fields: {', '.join(missing)}"}), 400
    if not isinstance(raw_order.get("raw_items"), list) or len(raw_order["raw_items"]) == 0:
        return jsonify({"error": "raw_items empty or not a list"}), 400

    # Run the same downstream pipeline the PDF flow runs after extraction.
    from app.domain.validation import validate_raw_order, validate_normalized_order
    from app.domain.normalize import normalize_order
    from app.domain.kitchen_engine import build_kitchen_result
    from app.domain.ticket_context import build_ticket_context
    from app.domain.master_sheet_map import build_all_outputs
    from app.services.dispatch_planner import build_dispatch_plans
    from app.services.orders_service import catalog as _catalog

    raw_warnings = validate_raw_order(raw_order)
    try:
        normalized = normalize_order(raw_order, _catalog)
    except Exception as e:
        logger.exception("normalize failed for structured ingest")
        return jsonify({"success": False, "stage": "normalizing_order", "error": str(e)}), 422

    norm_warnings = validate_normalized_order(normalized)
    all_warnings = raw_warnings + norm_warnings

    try:
        kitchen_result = build_kitchen_result(normalized)
    except Exception as e:
        logger.exception("kitchen rules failed")
        return jsonify({"success": False, "stage": "building_result", "error": str(e)}), 422

    try:
        dispatch_plans = build_dispatch_plans([normalized])
    except Exception as e:
        logger.warning("dispatch failed for structured ingest %s: %s", normalized.get("order_id"), e)
        dispatch_plans = {}
    dispatch = dispatch_plans.get(normalized.get("order_id"), {})
    normalized["route_group_id"] = dispatch.get("route_group_id")
    normalized["route_stop_index"] = dispatch.get("route_stop_index")
    normalized["assigned_driver"] = dispatch.get("assigned_driver")

    try:
        ctx = build_ticket_context(normalized, kitchen_result, dispatch)
        views = build_all_outputs(normalized, kitchen_result, ctx, _catalog)
    except Exception as e:
        logger.exception("post-processing failed")
        return jsonify({"success": False, "stage": "post_processing", "error": str(e)}), 422

    bundle = {
        "success": True,
        "pdf_path": "",  # no PDF for this path
        "order_id": normalized.get("order_id"),
        "raw_order": raw_order,
        "normalized_order": normalized,
        "kitchen_result": kitchen_result,
        "ticket_context": ctx,
        "views": views,
        "dispatch": dispatch,
        "warnings": all_warnings,
        "needs_review": bool(all_warnings),
        "processing_seconds": round(_time.time() - started, 2),
        # Pass-through ezCater identifiers for the unassign-courier flow.
        "external_delivery_id": raw_order.get("_external_delivery_id"),
    }

    job_db_id = persist_processing_job(1)
    persist_results(job_db_id, [bundle])

    return jsonify({
        "success": True,
        "order_id": normalized.get("order_id"),
        "needs_review": bundle["needs_review"],
        "warnings": all_warnings,
        "view_url": url_for("orders_browse.view_order",
                            external_order_id=normalized.get("order_id"),
                            _external=False),
        "processing_seconds": bundle["processing_seconds"],
    }), 200


@cater.route("/orders/ingest", methods=["POST"])
def ingest_order():
    """Auto-ingest endpoint for the AiCk ezcater agent. Accepts a single PDF
    and runs it through the same pipeline as /orders, but synchronously and
    without the browser-driven async job dance.

    Auth: Bearer token in Authorization header, matching INGEST_TOKEN env.
    Loopback usage only — token is not strong enough for public exposure
    (cloudflared tunnel routes /orders/ingest the same as anything else,
    so don't share the token).
    """
    expected = os.getenv("INGEST_TOKEN")
    if not expected:
        return jsonify({"error": "INGEST_TOKEN not configured on server"}), 500
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != expected:
        return jsonify({"error": "unauthorized"}), 401

    files = request.files.getlist("pdf")
    if not files or not files[0].filename:
        return jsonify({"error": "no pdf uploaded (expected multipart field 'pdf')"}), 400
    if len(files) > 1:
        return jsonify({"error": "only one pdf per request"}), 400

    pdf_paths = _save_uploaded_pdfs(files)
    if not pdf_paths:
        return jsonify({"error": "pdf rejected (size or extension)"}), 400

    pdf_path = pdf_paths[0]
    try:
        result = process_single_pdf(pdf_path)
        if not result.get("success"):
            return jsonify({
                "success": False,
                "stage": result.get("stage"),
                "error": result.get("error"),
            }), 422

        # Mirror the post-processing the multi-PDF flow does (dispatch + ticket
        # context). For a single ingest we don't bother with pairing — solo
        # dispatch only.
        from app.services.dispatch_planner import build_dispatch_plans
        from app.domain.ticket_context import build_ticket_context
        from app.domain.master_sheet_map import build_all_outputs
        from app.services.orders_service import catalog as _catalog

        normalized = result["normalized_order"]
        kitchen_result = result["kitchen_result"]
        try:
            dispatch_plans = build_dispatch_plans([normalized])
        except Exception as e:
            logger.warning("dispatch failed for ingested %s: %s", normalized.get("order_id"), e)
            dispatch_plans = {}
        dispatch = dispatch_plans.get(normalized.get("order_id"), {})
        normalized["route_group_id"] = dispatch.get("route_group_id")
        normalized["route_stop_index"] = dispatch.get("route_stop_index")
        normalized["assigned_driver"] = dispatch.get("assigned_driver")
        ctx = build_ticket_context(normalized, kitchen_result, dispatch)
        views = build_all_outputs(normalized, kitchen_result, ctx, _catalog)

        bundle = {
            **result,
            "ticket_context": ctx,
            "views": views,
            "dispatch": dispatch,
        }

        job_db_id = persist_processing_job(1)
        persist_results(job_db_id, [bundle])

        return jsonify({
            "success": True,
            "order_id": normalized.get("order_id"),
            "needs_review": result.get("needs_review", False),
            "warnings": result.get("warnings", []),
            "view_url": url_for("orders_browse.view_order",
                                external_order_id=normalized.get("order_id"),
                                _external=False),
        }), 200
    finally:
        _cleanup_files(pdf_paths)


@cater.route("/orders/result/<job_id>")
def job_result(job_id):
    job = _jobs.get(job_id)
    if not job or job["status"] != "done":
        return redirect(url_for("ezcater.orders"))
    result = job["result"]
    has_xlsx = result.get("success") and result.get("xlsx_bytes")
    collapse = result.get("collapse_empty_rows", False)
    return render_template(
        "orders.html",
        grids=result.get("grids"),
        orders=result.get("orders", []),
        active_view="master",
        collapse_empty_rows=collapse,
        error=result.get("error"),
        success_count=result.get("success_count", 0),
        failure_count=result.get("failure_count", 0),
        xlsx_job_id=job_id if has_xlsx else None,
    )
