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

import hashlib
import hmac
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
from app.web.dashboard_access import current_role_is, has_dashboard_access

log = logging.getLogger(__name__)

corp_order = Blueprint("corporate_order", __name__)
corp_order_public = Blueprint("corporate_order_public", __name__)

# Per-store URL prefix. Mirrors the same slug→location mapping as store_bp
# (app/web/store_routes.py); kept here so this blueprint is self-contained.
VALID_STORES = {"uno", "dos", "corporate", "partner"}
STORE_TO_LOCATION = {
    "uno": "copperfield", "dos": "tomball",
    "corporate": "both",  "partner": "both",
}
# Mirror store_routes.STORE_LABELS so the sidebar's "{{ store_label }}" doesn't
# fall back to its 'Tomball' default when rendering a /<store>/corporate-order
# page that's served by THIS blueprint (not store_bp).
STORE_LABELS = {
    "dos": "Tomball", "uno": "Copperfield",
    "corporate": "Corporate", "partner": "Partner",
}

ORDER_PORTAL_PROFILES = {
    "corporate": {
        "label": "Corporate",
        "title": "Admin",
        "store_slug": "corporate",
        "store_key": "corporate",
        "hash": "0225e993a1e6566a7af96340f7329903b625882c7a029611b092ec7d1cd9c9ba",
    },
    "tomball": {
        "label": "Tomball",
        "title": "Store",
        "store_slug": "dos",
        "store_key": "tomball",
        "hash": "f8a32455c8cf7a9f3bfd25de132490d5569ddd2ed570da35a6f625c245f44ee6",
    },
    "copperfield": {
        "label": "Copperfield",
        "title": "Store",
        "store_slug": "uno",
        "store_key": "copperfield",
        "hash": "57bff3d61f312f22ff2bb9eb5f69f8547f6bf41f52290a40f6938b262ca538fb",
    },
}
ORDER_PROFILE_SCOPES_BY_CONTEXT = {
    "dos": ("corporate", "tomball"),
    "uno": ("corporate", "copperfield"),
    "corporate": ("corporate", "tomball", "copperfield"),
    "partner": ("corporate", "tomball", "copperfield"),
}
PIN_SCOPE_BY_SLUG = {
    profile["store_slug"]: scope
    for scope, profile in ORDER_PORTAL_PROFILES.items()
}
TAKEOUT_CATERING_CATEGORY = "Take-out & Catering"
CUPS_LIDS_CATEGORY = "Cups & Lids"
FOH_CATEGORY = "FOH"
OFFICE_UNIFORMS_CATEGORY = "Office & Uniforms"
BOH_CATEGORY = "BOH"
LEGACY_CATEGORY_ALIASES = {
    "1-3 Compartment Containers": TAKEOUT_CATERING_CATEGORY,
    "Aluminum Foil Pans & Containers": TAKEOUT_CATERING_CATEGORY,
    "Togo & Catering": TAKEOUT_CATERING_CATEGORY,
    "Foam Cups and Lids": CUPS_LIDS_CATEGORY,
    "Portion Cup & Lids": CUPS_LIDS_CATEGORY,
    "Server": FOH_CATEGORY,
    "Host & Togo": FOH_CATEGORY,
    "Bar": FOH_CATEGORY,
    "Office": OFFICE_UNIFORMS_CATEGORY,
    "Uniforms": OFFICE_UNIFORMS_CATEGORY,
    "Cleaning Supplies": BOH_CATEGORY,
    "Spices": BOH_CATEGORY,
}
CURRENT_ORDER_STATUSES = {"Submitted", "In Progress"}


def _pin_digest(scope: str, pin: str) -> str:
    return hashlib.sha256(f"cenas-corporate-order:{scope}:{pin}".encode()).hexdigest()


def _profile_for_slug(slug: str | None) -> dict | None:
    scope = PIN_SCOPE_BY_SLUG.get(slug or "")
    return ORDER_PORTAL_PROFILES.get(scope) if scope else None


def _corp_order_url_for_scope(scope: str) -> str:
    profile = ORDER_PORTAL_PROFILES[scope]
    return url_for("corporate_order.view", store_slug=profile["store_slug"])


def _portal_profiles_for_context(store_context: str | None, target: str | None = None) -> dict:
    """Return the PIN cards visible for the selected Operations store scope."""
    context = (store_context or "").strip().lower()
    if context not in ORDER_PROFILE_SCOPES_BY_CONTEXT:
        if target == "tomball":
            context = "dos"
        elif target == "copperfield":
            context = "uno"
        elif target == "corporate":
            context = "corporate"
    scopes = ORDER_PROFILE_SCOPES_BY_CONTEXT.get(
        context,
        tuple(ORDER_PORTAL_PROFILES.keys()),
    )
    return {scope: ORDER_PORTAL_PROFILES[scope] for scope in scopes}


def _valid_platform_session(slug: str | None) -> bool:
    profile = _profile_for_slug(slug)
    if not profile:
        return False
    return session.get("corporate_order_scope") == profile["store_slug"]


def _platform_login_url(slug: str | None = None) -> str:
    profile = _profile_for_slug(slug)
    target = ""
    if profile:
        for scope, cand in ORDER_PORTAL_PROFILES.items():
            if cand is profile:
                target = scope
                break
    return url_for(
        "corporate_order_public.entry",
        target=target,
        next=request.full_path if request.query_string else request.path,
    )


def _order_analytics(orders: list[dict]) -> dict:
    by_store: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total_units = 0
    total_fulfilled = 0
    for order in orders or []:
        store = order.get("store_key") or "unknown"
        status = order.get("status") or "Unknown"
        by_store[store] = by_store.get(store, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        total_units += int(order.get("total_quantity") or 0)
        total_fulfilled += int(order.get("total_fulfilled") or 0)
    return {
        "total_orders": len(orders or []),
        "total_units": total_units,
        "total_fulfilled": total_fulfilled,
        "total_open": max(0, total_units - total_fulfilled),
        "by_store": sorted(by_store.items()),
        "by_status": sorted(by_status.items()),
    }


@corp_order_public.route("/corporate-order", methods=["GET"])
def entry():
    target = (request.args.get("target") or "").strip().lower()
    if target not in ORDER_PORTAL_PROFILES:
        target = ""
    store_context = (request.args.get("store_context") or "").strip().lower()
    if store_context not in ORDER_PROFILE_SCOPES_BY_CONTEXT:
        store_context = ""
    if not target and request.args.get("switch") != "1" and not store_context:
        active_slug = session.get("corporate_order_scope")
        for scope, profile in ORDER_PORTAL_PROFILES.items():
            if profile["store_slug"] == active_slug:
                return redirect(_corp_order_url_for_scope(scope))
    return render_template(
        "corporate_order_entry.html",
        profiles=_portal_profiles_for_context(store_context, target),
        selected=target,
        error=None,
        next_url=request.args.get("next") or "",
        store_context=store_context,
    )


@corp_order_public.route("/corporate-order/login", methods=["POST"])
def portal_login():
    scope = (request.form.get("scope") or "").strip().lower()
    pin = (request.form.get("pin") or "").strip()
    nxt = (request.form.get("next") or "").strip()
    store_context = (request.form.get("store_context") or "").strip().lower()
    if store_context not in ORDER_PROFILE_SCOPES_BY_CONTEXT:
        store_context = ""
    profile = ORDER_PORTAL_PROFILES.get(scope)
    if (
        profile is None
        or len(pin) != 4
        or not pin.isdigit()
        or not hmac.compare_digest(_pin_digest(scope, pin), profile["hash"])
    ):
        return render_template(
            "corporate_order_entry.html",
            profiles=_portal_profiles_for_context(store_context, scope),
            selected=scope if scope in ORDER_PORTAL_PROFILES else "",
            error="That code did not match. Try again.",
            next_url=nxt,
            store_context=store_context,
        ), 401

    session["corporate_order_scope"] = profile["store_slug"]
    session.permanent = True
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = _corp_order_url_for_scope(scope)
    allowed_path = f"/{profile['store_slug']}/corporate-order"
    if not nxt.startswith(allowed_path):
        nxt = allowed_path
    return redirect(nxt)


@corp_order_public.route("/corporate-order/logout", methods=["POST"])
def portal_logout():
    session.pop("corporate_order_scope", None)
    return redirect(url_for("corporate_order_public.entry"))


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
    g.store_label = STORE_LABELS[slug]


@corp_order.url_defaults
def _inject_store(endpoint, values):
    if "store_slug" not in values and getattr(g, "current_store", None):
        values["store_slug"] = g.current_store


@corp_order.before_request
def _partner_gate():
    """Keep legacy /partner/corporate-order from bypassing the PIN portal."""
    if getattr(g, "current_store", None) == "partner":
        return None
    if _valid_platform_session(getattr(g, "current_store", None)):
        return None


@corp_order.before_request
def _dashboard_gate():
    """Corporate Order is the only Operations sub-tab Expo may reach."""
    target = getattr(g, "current_store", None)
    if target:
        session["last_store_slug"] = target
    if target == "partner":
        return redirect(url_for(
            "corporate_order_public.entry",
            target="corporate",
            next=url_for("corporate_order.view", store_slug="corporate"),
        ))
    if target in PIN_SCOPE_BY_SLUG and _valid_platform_session(target):
        return None
    if target in PIN_SCOPE_BY_SLUG:
        return redirect(_platform_login_url(target))

    from app.web.permissions import accessible_store_slugs

    user = getattr(g, "current_user", None)
    if user is not None and user.permission_level not in ("partner", "corporate"):
        allowed = accessible_store_slugs(user)
        if target not in allowed:
            if allowed:
                return redirect(f"/{allowed[0]}/")
            return ("Forbidden — your account isn't assigned to this store.", 403)

    if not has_dashboard_access("dash.operations", target):
        if target in PIN_SCOPE_BY_SLUG:
            return redirect(_platform_login_url(target))
        abort(403)
    if current_role_is("expo") and request.endpoint not in (
        "corporate_order.view",
        "corporate_order.submit",
    ):
        abort(403)

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


def _shop_store_key() -> str:
    """Translate URL slugs to the synthetic Customer keys in corporate_shop."""
    slug = getattr(g, "current_store", None)
    if slug == "dos":
        return "tomball"
    if slug == "uno":
        return "copperfield"
    return slug or "corporate"


def _current_category() -> str:
    selected_category = (request.values.get("category") or "").strip()
    return LEGACY_CATEGORY_ALIASES.get(selected_category, selected_category)


def _catalog_redirect(category: str | None = None):
    category = (category or "").strip()
    if category:
        return redirect(url_for(
            "corporate_order.view",
            store_slug=g.current_store,
            category=category,
        ))
    return redirect(url_for("corporate_order.view", store_slug=g.current_store))


def _parse_qty_items() -> list[tuple[int, int, int | None]]:
    items: list[tuple[int, int, int | None]] = []
    for key, value in request.form.items():
        if not key.startswith("qty_"):
            continue
        try:
            product_id = int(key.removeprefix("qty_"))
            quantity = int(value)
        except ValueError:
            continue
        if quantity > 0:
            store_on_hand = None
            raw_on_hand = request.form.get(f"oh_{product_id}")
            if raw_on_hand not in (None, ""):
                try:
                    store_on_hand = max(0, int(raw_on_hand or 0))
                except ValueError:
                    store_on_hand = None
            items.append((product_id, quantity, store_on_hand))
    return items


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
        oh_text = (
            f"  [store OH: {it.get('store_on_hand')}]"
            if it.get("store_on_hand") is not None else ""
        )
        plain_lines.append(
            f"  - {it['quantity']} × {it['name']} ({it.get('category','-')})"
            + oh_text
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
            f"<td>{it.get('store_on_hand','') if it.get('store_on_hand') is not None else ''}</td>"
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
        "<th style='padding:6px 10px;text-align:left'>Store OH</th>"
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
            order_add_target=None,
            platform_mode=True,
            portal_profiles=ORDER_PORTAL_PROFILES,
            analytics=_order_analytics([]),
        )

    try:
        corporate_shop.ensure_catalog_seeded()
    except Exception:
        log.exception("corporate_order: catalog seed check failed")

    selected_category = _current_category()
    products = corporate_shop.list_products(category=selected_category or None)
    categories = corporate_shop.list_categories()

    # Admins (corporate / partner) see all orders; stores see their own.
    if _is_admin():
        orders = corporate_shop.list_orders(limit=None)
    else:
        orders = corporate_shop.list_orders(limit=25, store_filter=_shop_store_key())

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
        order_add_target=None,
        platform_mode=True,
        portal_profiles=ORDER_PORTAL_PROFILES,
        analytics=_order_analytics(orders),
    )


@corp_order.route("/corporate-order/orders", methods=["GET"])
def orders():
    """Store-facing current orders list. Stores can add to open orders here."""
    if _is_admin():
        return redirect(url_for("corporate_order.reports", store_slug=g.current_store))
    if not corporate_shop.is_configured():
        return render_template(
            "corporate_order_orders.html",
            active="corporate_order_orders",
            page_title="Corporate Order — Orders",
            configured=False,
            is_admin=False,
            orders=[],
            current_orders=[],
            platform_mode=True,
        )
    rows = corporate_shop.list_orders(limit=None, store_filter=_shop_store_key())
    current_orders = [
        order for order in rows
        if (order.get("status") or "Submitted") in CURRENT_ORDER_STATUSES
    ]
    return render_template(
        "corporate_order_orders.html",
        active="corporate_order_orders",
        page_title="Corporate Order — Orders",
        configured=True,
        is_admin=False,
        orders=rows,
        current_orders=current_orders,
        platform_mode=True,
    )


@corp_order.route("/corporate-order/orders/<int:order_id>/add", methods=["GET", "POST"])
def add_to_order(order_id):
    """Store-facing flow for adding more items to an existing open order."""
    if _is_admin():
        abort(403)
    if not corporate_shop.is_configured():
        flash("Corporate Order DB is not connected. Set CORPORATE_DB_URL on Render.", "error")
        return redirect(url_for("corporate_order.orders", store_slug=g.current_store))
    order = corporate_shop.get_order(order_id, store_filter=_shop_store_key())
    if not order:
        flash(f"Order #{order_id} was not found for {g.store_label}.", "error")
        return redirect(url_for("corporate_order.orders", store_slug=g.current_store))
    if (order.get("status") or "Submitted") not in CURRENT_ORDER_STATUSES:
        flash(f"Order #{order_id} is closed and cannot be changed.", "warning")
        return redirect(url_for("corporate_order.orders", store_slug=g.current_store))

    if request.method == "POST":
        items = _parse_qty_items()
        if not items:
            flash("No items selected — pick a quantity > 0 on at least one product.", "warning")
            return redirect(url_for(
                "corporate_order.add_to_order",
                store_slug=g.current_store,
                order_id=order_id,
                category=_current_category(),
            ))
        try:
            added = corporate_shop.add_items_to_order(order_id, _shop_store_key(), items)
        except Exception as ex:
            log.exception("corporate_order: add_to_order failed")
            flash(f"Could not add to order #{order_id}: {ex}", "error")
            return redirect(url_for(
                "corporate_order.add_to_order",
                store_slug=g.current_store,
                order_id=order_id,
                category=_current_category(),
            ))
        total_qty = sum(int(item.get("quantity") or 0) for item in added.get("items") or [])
        flash(
            f"Added {total_qty} item{'s' if total_qty != 1 else ''} to order #{order_id}.",
            "success",
        )
        return redirect(url_for("corporate_order.orders", store_slug=g.current_store))

    try:
        corporate_shop.ensure_catalog_seeded()
    except Exception:
        log.exception("corporate_order: catalog seed check failed")
    selected_category = _current_category()
    products = corporate_shop.list_products(category=selected_category or None)
    categories = corporate_shop.list_categories()
    store_orders = corporate_shop.list_orders(limit=25, store_filter=_shop_store_key())
    return render_template(
        "corporate_order.html",
        active="corporate_order_orders",
        page_title="Corporate Order — Add to Order",
        configured=True,
        is_admin=False,
        products=products,
        categories=categories,
        selected_category=selected_category,
        orders=store_orders,
        recent_submission=None,
        order_add_target=order,
        platform_mode=True,
        portal_profiles=ORDER_PORTAL_PROFILES,
        analytics=_order_analytics(store_orders),
    )


@corp_order.route("/corporate-order/submit", methods=["POST"])
def submit():
    """Build order from posted qty inputs (name='qty_<product_id>') and place it."""
    if not corporate_shop.is_configured():
        flash("Corporate Order DB is not connected. Set CORPORATE_DB_URL on Render.", "error")
        return redirect(url_for("corporate_order.view", store_slug=g.current_store))

    items = _parse_qty_items()

    if not items:
        flash("No items selected — pick a quantity > 0 on at least one product.", "warning")
        return redirect(url_for("corporate_order.view", store_slug=g.current_store))

    try:
        order = corporate_shop.place_order(_shop_store_key(), items)
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
            analytics=_order_analytics([]),
            platform_mode=True,
        )
    if _is_admin():
        orders = corporate_shop.list_orders(limit=None)
    else:
        orders = corporate_shop.list_orders(limit=None, store_filter=_shop_store_key())

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
        analytics=_order_analytics(orders),
        platform_mode=True,
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
    fulfilled: dict[int, int] = {}
    for key, raw in request.form.items():
        if not key.startswith("fulfilled_"):
            continue
        try:
            line_id = int(key.removeprefix("fulfilled_"))
            qty = int(raw or 0)
        except ValueError:
            continue
        fulfilled[line_id] = qty
    ok = corporate_shop.update_order_fulfillment(
        order_id,
        fulfilled,
        new_status=new_status,
    )
    if not ok:
        flash(f"Order #{order_id} not found.", "error")
    else:
        flash(f"Order #{order_id}: fulfillment saved and status set to {new_status}.", "success")
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
    return _catalog_redirect(_current_category())


@corp_order.route("/corporate-order/admin/product/<int:product_id>/stock-adjust",
                  methods=["POST"])
def adjust_stock(product_id):
    """Admin-only: add or subtract from the on-hand stock count."""
    if not _is_admin():
        abort(403)
    try:
        delta = int(request.form.get("stock_delta") or "0")
    except ValueError:
        flash("ADD must be a whole number, such as 5 or -2.", "error")
        return _catalog_redirect(_current_category())
    new_stock = corporate_shop.adjust_stock(product_id, delta)
    if new_stock is None:
        flash(f"Product #{product_id} not found.", "error")
    else:
        flash(f"Product #{product_id}: OH adjusted by {delta:+d}; now {new_stock}.", "success")
    return _catalog_redirect(_current_category())


@corp_order.route("/corporate-order/admin/products/order", methods=["POST"])
def update_product_order():
    """Admin-only: save display order for one department."""
    if not _is_admin():
        abort(403)
    category = _current_category()
    raw_ids = request.form.get("product_order") or ""
    product_ids = []
    for raw in raw_ids.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            product_ids.append(int(raw))
        except ValueError:
            continue
    try:
        count = corporate_shop.update_product_order(category, product_ids)
    except Exception as ex:
        log.exception("corporate_order: update product order failed")
        flash(f"Could not save item order: {ex}", "error")
        return _catalog_redirect(category)
    flash(f"{category}: saved display order for {count} item{'s' if count != 1 else ''}.", "success")
    return _catalog_redirect(category)


@corp_order.route("/corporate-order/admin/product/add", methods=["POST"])
def add_product():
    """Admin-only: add a new item to the live corporate order catalog."""
    if not _is_admin():
        abort(403)
    name = request.form.get("name") or ""
    category = request.form.get("category") or ""
    picture = request.form.get("picture") or ""
    try:
        in_stock = int(request.form.get("in_stock") or "0")
        product = corporate_shop.add_product(
            name=name,
            category=category,
            in_stock=in_stock,
            picture=picture,
        )
    except Exception as ex:
        flash(f"Could not add item: {ex}", "error")
        return _catalog_redirect(_current_category())
    flash(f"Added {product['name']} to the corporate catalog.", "success")
    return _catalog_redirect(category)


@corp_order.route("/corporate-order/admin/product/<int:product_id>/delete", methods=["POST"])
def delete_product(product_id):
    """Admin-only: remove an item from the live corporate order catalog."""
    if not _is_admin():
        abort(403)
    ok = corporate_shop.delete_product(product_id)
    if ok:
        flash(f"Product #{product_id} removed from the corporate catalog.", "success")
    else:
        flash(f"Product #{product_id} not found.", "error")
    return _catalog_redirect(_current_category())
