"""Partner-only ezCater XLSX import page.

Sam exports the Caterer Portal "Order Data" report (Order Number + Food Total
+ Caterer Total Due + per-store breakdown) and drops it on /partner/developer/
ezcater-import. We parse it, match Order Number → Order.external_order_id,
and update Order.total_amount with the canonical Food Total. Orders not yet
in our DB get stub rows so reports include them.
"""
from __future__ import annotations

import logging

from flask import Blueprint, render_template, request, redirect, url_for, session

log = logging.getLogger(__name__)

ezc_import = Blueprint("ezcater_import", __name__)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB; the typical export is < 100 KB


def _enforce_partner():
    if not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login"))
    return None


@ezc_import.route("/partner/developer/ezcater-import", methods=["GET"])
def page():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    return render_template(
        "ezcater_import.html",
        result=None,
        error=request.args.get("error"),
    )


@ezc_import.route("/partner/developer/ezcater-import", methods=["POST"])
def upload():
    gate = _enforce_partner()
    if gate is not None:
        return gate
    file = request.files.get("xlsx")
    if not file or not file.filename:
        return redirect(url_for("ezcater_import.page", error="No file selected."))
    if not file.filename.lower().endswith(".xlsx"):
        return redirect(url_for("ezcater_import.page", error="Pick an .xlsx export."))
    file.stream.seek(0, 2)
    if file.stream.tell() > MAX_UPLOAD_BYTES:
        return redirect(url_for("ezcater_import.page", error=f"File too large (max {MAX_UPLOAD_BYTES // 1024} KB)."))
    file.stream.seek(0)
    try:
        from app.services.ezcater_import import parse_export_xlsx, apply_import
        rows = parse_export_xlsx(file.stream)
        result = apply_import(rows)
    except ValueError as e:
        return redirect(url_for("ezcater_import.page", error=str(e)))
    except Exception:
        log.exception("ezCater import failed")
        return redirect(url_for("ezcater_import.page",
                                error="Parse failed — check the file is the 'Order Data' report from the Caterer Portal."))
    return render_template("ezcater_import.html", result=result, error=None)
