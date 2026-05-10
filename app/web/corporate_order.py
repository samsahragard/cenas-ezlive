"""Corporate Order blueprint — Partner-only admin + per-store browse/place.

URL surface (mounted under store_bp's `/<store_slug>` prefix):
    /<store>/corporate-order        — UI (catalog + cart for stores; admin for partner/corporate)
    /<store>/corporate-order/submit — POST (place an order from the cart)
    /<store>/corporate-order/admin/order/<id>/status — POST (mark fulfilled, partner/corporate only)

Per-store permissions auto-derive from g.current_store:
    'tomball', 'copperfield' → can browse + place orders
    'corporate'              → full admin (catalog + all orders + status updates)
    'partner'                → same as corporate (Sam said 'all permissions under partner')
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, g, jsonify, session

from app.services import corporate_shop

log = logging.getLogger(__name__)

corp_order = Blueprint("corporate_order", __name__)

# Per-store URL prefix. Mirrors the same slug→location mapping as store_bp
# (app/web/store_routes.py); kept here so this blueprint is self-contained.
VALID_STORES = {"uno", "dos", "corporate", "partner"}
STORE_TO_LOCATION = {
    "uno": "copperfield", "dos": "tomball",
    "corporate": "both",  "partner": "both",
}


@corp_order.url_value_preprocessor
def _pull_store(endpoint, values):
    if values is None:
        return
    slug = values.pop("store_slug", None)
    if slug is None:
        return
    if slug not in VALID_STORES:
        abort(404)
    g.current_store = slug
    g.current_location = STORE_TO_LOCATION[slug]


@corp_order.url_defaults
def _inject_store(endpoint, values):
    if "store_slug" not in values and getattr(g, "current_store", None):
        values["store_slug"] = g.current_store


@corp_order.before_request
def _partner_gate():
    """Mirror store_bp's _partner_gate so /partner/corporate-order requires the
    second password too. /uno/, /dos/, /corporate/ rely solely on the site
    EZLIVE_PASSWORD already enforced by auth.py."""
    if getattr(g, "current_store", None) == "partner" and not session.get("partner_auth_ok"):
        return redirect(url_for("auth.partner_login"))

# Email recipient(s) for new corporate orders. Defaults to Sam; override via
# CORPORATE_ORDER_TO env var (comma-separated list) to add Masood etc. later.
DEFAULT_RECIPIENT = "samsahragard@gmail.com"

# SMTP config — same setup the produce_order blueprint uses.
SMTP_HOST = os.getenv("ORDERS_SMTP_HOST", "gvam1078.siteground.biz")
SMTP_PORT = int(os.getenv("ORDERS_SMTP_PORT", "465"))
SMTP_USER = os.getenv("ORDERS_SMTP_USER", "orders@cenaskitchen.com")
FROM_NAME = "Cenas Kitchen Corporate Orders"

# Local-AiCk fallback for the SMTP password (Render uses ORDERS_EMAIL_PWD env var).
_AICK_SECRETS = Path(r"C:\Users\sam\.openclaw\.secrets")


def _email_pwd() -> str | None:
    val = os.getenv("ORDERS_EMAIL_PWD")
    if val:
        return val.strip()
    f = _AICK_SECRETS / "orders_smtp_pwd.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return None


def _is_admin() -> bool:
    """Corporate + Partner have admin rights; Tomball + Copperfield don't."""
    return getattr(g, "current_store", None) in ("corporate", "partner")


def _send_corporate_order_email(order: dict) -> tuple[bool, str]:
    """Email Sam + any additional recipients about a newly-placed corporate order.
    Returns (sent_ok, error_or_blank)."""
    pwd = _email_pwd()
    if not pwd:
        return False, "ORDERS_EMAIL_PWD not configured — order saved but email skipped"
    to_csv = os.getenv("CORPORATE_ORDER_TO", DEFAULT_RECIPIENT)
    recipients = [r.strip() for r in to_csv.split(",") if r.strip()]
    if not recipients:
        return False, "no recipients configured"

    store = order.get("store_label") or "(unknown store)"
    subj = f"Corporate Order #{order['order_id']} from {store}"
    submitted_ct = order.get("submitted_at")
    if isinstance(submitted_ct, datetime):
        if submitted_ct.tzinfo is None:
            submitted_ct = submitted_ct.replace(tzinfo=timezone.utc)
        submitted_ct = submitted_ct.astimezone(timezone(timedelta(hours=-5))).strftime("%a %b %d, %I:%M %p CT")
    elif submitted_ct:
        submitted_ct = str(submitted_ct)
    else:
        submitted_ct = "(unknown time)"

    items = order.get("items") or []
    plain_lines = [
        f"Corporate Order #{order['order_id']}",
        f"From:  {store}",
        f"When:  {submitted_ct}",
        "",
        "Items:",
    ]
    for it in items:
        plain_lines.append(
            f"  - {it['quantity']} × {it['name']} ({it.get('category','-')})"
            + (f"  [stock now: {it.get('remaining_stock','?')}]" if it.get('remaining_stock') is not None else "")
        )
    plain_lines.append("")
    plain_lines.append("Open the dashboard to fulfill: https://app.cenaskitchen.com/corporate/corporate-order")
    plain = "\n".join(plain_lines)

    rows_html = []
    for it in items:
        rows_html.append(
            f"<tr><td>{it['quantity']}</td>"
            f"<td>{it['name']}</td>"
            f"<td>{it.get('category','')}</td>"
            f"<td style='color:#666'>{it.get('remaining_stock','')}</td></tr>"
        )
    html = (
        "<html><body style='font-family:-apple-system,Helvetica,sans-serif;color:#222;'>"
        f"<h2 style='color:#0a0604'>Corporate Order #{order['order_id']}</h2>"
        f"<p><strong>From:</strong> {store}<br>"
        f"<strong>When:</strong> {submitted_ct}</p>"
        "<table style='border-collapse:collapse;font-size:14px'>"
        "<thead><tr style='background:#f4f4f4'>"
        "<th style='padding:6px 10px;text-align:left'>Qty</th>"
        "<th style='padding:6px 10px;text-align:left'>Item</th>"
        "<th style='padding:6px 10px;text-align:left'>Category</th>"
        "<th style='padding:6px 10px;text-align:left'>Stock now</th>"
        "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>"
        f"<p><a href='https://app.cenaskitchen.com/corporate/corporate-order'>"
        "Open the dashboard to fulfill →</a></p>"
        "</body></html>"
    )

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subj
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as srv:
            srv.login(SMTP_USER, pwd)
            srv.sendmail(SMTP_USER, recipients, msg.as_string())
        log.info("corporate_order: email sent to %s", recipients)
        return True, ""
    except Exception as ex:
        log.exception("corporate_order: SMTP send failed")
        return False, str(ex)


@corp_order.route("/corporate-order", methods=["GET"])
def view():
    """Catalog + cart UI. Per-store permissions auto from g.current_store
    set by store_bp.url_value_preprocessor."""
    if not corporate_shop.is_configured():
        return render_template(
            "corporate_order.html",
            active="corporate_order",
            page_title="Corporate Order",
            configured=False,
            is_admin=_is_admin(),
            products=[], categories=[], orders=[],
            recent_submission=None,
        )

    selected_category = (request.args.get("category") or "").strip()
    products = corporate_shop.list_products(category=selected_category or None)
    categories = corporate_shop.list_categories()

    # Admins (corporate / partner) see all orders; stores see their own.
    if _is_admin():
        orders = corporate_shop.list_orders(limit=30)
    else:
        orders = corporate_shop.list_orders(limit=10, store_filter=g.current_store)

    flash_id = request.args.get("submitted")
    return render_template(
        "corporate_order.html",
        active="corporate_order",
        page_title="Corporate Order",
        configured=True,
        is_admin=_is_admin(),
        products=products,
        categories=categories,
        selected_category=selected_category,
        orders=orders,
        recent_submission=flash_id,
    )


@corp_order.route("/corporate-order/submit", methods=["POST"])
def submit():
    """Build order from posted qty inputs (name='qty_<product_id>') and place it."""
    if not corporate_shop.is_configured():
        flash("Corporate Order DB is not connected. Set CORPORATE_DB_URL on Render.", "error")
        return redirect(url_for("corporate_order.view", store_slug=g.current_store))

    items: list[tuple[int, int]] = []
    for k, v in request.form.items():
        if not k.startswith("qty_"):
            continue
        try:
            pid = int(k.removeprefix("qty_"))
            qty = int(v)
        except ValueError:
            continue
        if qty > 0:
            items.append((pid, qty))

    if not items:
        flash("No items selected — pick a quantity > 0 on at least one product.", "warning")
        return redirect(url_for("corporate_order.view", store_slug=g.current_store))

    try:
        order = corporate_shop.place_order(g.current_store, items)
    except Exception as ex:
        log.exception("corporate_order: place_order failed")
        flash(f"Could not place order: {ex}", "error")
        return redirect(url_for("corporate_order.view", store_slug=g.current_store))

    sent_ok, err_msg = _send_corporate_order_email(order)
    if sent_ok:
        flash(f"Order #{order['order_id']} placed and emailed to corporate.", "success")
    else:
        flash(f"Order #{order['order_id']} placed; email skipped ({err_msg}).", "warning")

    return redirect(url_for("corporate_order.view",
                            store_slug=g.current_store,
                            submitted=order["order_id"]))


@corp_order.route("/corporate-order/reports", methods=["GET"])
def reports():
    """Order history + light analytics. Stores see their own orders; admins
    (corporate / partner) see all orders + cross-store aggregates."""
    if not corporate_shop.is_configured():
        return render_template(
            "corporate_order_reports.html",
            active="corporate_order_reports",
            page_title="Corporate Order — Reports",
            configured=False,
            is_admin=_is_admin(),
            orders=[],
            top_products=[],
            by_store=[],
            by_status=[],
        )
    if _is_admin():
        orders = corporate_shop.list_orders(limit=200)
    else:
        orders = corporate_shop.list_orders(limit=200, store_filter=g.current_store)

    # Light aggregates from the in-memory list — cheap for ~200 rows.
    from collections import Counter, defaultdict
    by_status = Counter(o.get("status") or "?" for o in orders)
    by_store = Counter(o.get("store_key") or "?" for o in orders)
    item_qty = defaultdict(int)
    for o in orders:
        for line in o.get("lines") or []:
            item_qty[line["name"]] += line["quantity"]
    top_products = sorted(
        ({"name": n, "qty": q} for n, q in item_qty.items()),
        key=lambda r: -r["qty"],
    )[:10]
    by_status_list = sorted(({"label": k, "count": v} for k, v in by_status.items()),
                            key=lambda r: -r["count"])
    by_store_list = sorted(({"label": k, "count": v} for k, v in by_store.items()),
                           key=lambda r: -r["count"])
    return render_template(
        "corporate_order_reports.html",
        active="corporate_order_reports",
        page_title="Corporate Order — Reports",
        configured=True,
        is_admin=_is_admin(),
        orders=orders,
        top_products=top_products,
        by_store=by_store_list,
        by_status=by_status_list,
    )


@corp_order.route("/corporate-order/admin/order/<int:order_id>/status",
                  methods=["POST"])
def update_status(order_id):
    """Admin-only: mark order Fulfilled / Cancelled / In Progress."""
    if not _is_admin():
        abort(403)
    new_status = (request.form.get("status") or "").strip()
    if new_status not in ("Submitted", "In Progress", "Fulfilled", "Cancelled"):
        flash(f"Invalid status: {new_status!r}", "error")
        return redirect(url_for("corporate_order.view", store_slug=g.current_store))
    ok = corporate_shop.update_order_status(order_id, new_status)
    if not ok:
        flash(f"Order #{order_id} not found.", "error")
    else:
        flash(f"Order #{order_id} → {new_status}.", "success")
    return redirect(url_for("corporate_order.view", store_slug=g.current_store))


@corp_order.route("/corporate-order/admin/product/<int:product_id>/stock",
                  methods=["POST"])
def update_stock(product_id):
    """Admin-only: adjust stock count from the admin grid."""
    if not _is_admin():
        abort(403)
    try:
        new_stock = int(request.form.get("in_stock") or "0")
    except ValueError:
        flash("in_stock must be a whole number.", "error")
        return redirect(url_for("corporate_order.view", store_slug=g.current_store))
    ok = corporate_shop.update_stock(product_id, new_stock)
    if ok:
        flash(f"Product #{product_id}: in_stock set to {new_stock}.", "success")
    else:
        flash(f"Product #{product_id} not found.", "error")
    return redirect(url_for("corporate_order.view", store_slug=g.current_store))
