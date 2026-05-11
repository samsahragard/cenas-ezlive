"""Produce-order Blueprint.

Mounted at /produce. Kitchen managers see one row per canonical item with
the cheaper vendor's price + a qty input. On submit, the cart is grouped
by vendor and SMTP'd to each vendor's email (per vendor_routing.json),
followed by a manager confirmation email.

If the manager's selected location doesn't match their default
(e.g. Gina selecting Copperfield), the order is held in pending_orders.json
and Sam is Telegram'd three tappable links: approve / override / cancel.

State files live under PRODUCE_STATE_DIR (default /var/data/produce on
Render, or <repo>/instance/produce/ locally). Static config (managers,
locations, vendor_routing, canonical_items) is committed under
data/produce/ in the repo.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, url_for

logger = logging.getLogger(__name__)

produce_order = Blueprint(
    "produce_order",
    __name__,
    url_prefix="/produce",
)

# ============ Paths ============
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.getenv("PRODUCE_CONFIG_DIR") or (REPO_ROOT / "data" / "produce"))
STATE_DIR = Path(os.getenv("PRODUCE_STATE_DIR") or (REPO_ROOT / "instance" / "produce"))

CANONICAL_FILE = CONFIG_DIR / "canonical_items.json"
MANAGERS_FILE = CONFIG_DIR / "managers.json"
LOCATIONS_FILE = CONFIG_DIR / "locations.json"
VENDOR_ROUTING_FILE = CONFIG_DIR / "vendor_routing.json"

ALVARADO_FILE = STATE_DIR / "alvarado.json"
JLUNA_FILE = STATE_DIR / "jluna.json"
PENDING_ORDERS_FILE = STATE_DIR / "pending_orders.json"
COMPLETED_ORDERS_FILE = STATE_DIR / "completed_orders.json"

# ============ SMTP / Telegram config ============
SMTP_HOST = os.getenv("ORDERS_SMTP_HOST", "gvam1078.siteground.biz")
SMTP_PORT = int(os.getenv("ORDERS_SMTP_PORT", "465"))
SMTP_USER = os.getenv("ORDERS_SMTP_USER", "orders@cenaskitchen.com")
FROM_NAME = "Cenas Kitchen Orders"

SAM_TELEGRAM_CHAT_ID = os.getenv("PRODUCE_TG_CHAT_ID", "8612324971")
TELEGRAM_API_BASE = "https://api.telegram.org"

# Fallback file paths for local AiCk dev — env vars win on Render.
_AICK_SECRETS = Path(r"C:\Users\sam\.openclaw\.secrets")


def _email_pwd() -> str:
    """SMTP/IMAP password for orders@cenaskitchen.com."""
    val = os.getenv("ORDERS_EMAIL_PWD")
    if val:
        return val.strip()
    f = _AICK_SECRETS / "orders_smtp_pwd.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    raise RuntimeError("missing ORDERS_EMAIL_PWD env var and fallback file")


def _tg_token() -> str | None:
    val = os.getenv("TELEGRAM_BOT_TOKEN")
    if val:
        return val.strip()
    f = _AICK_SECRETS / "ck_telegram_bot_token.txt"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
    return None


# ============ Helpers ============
def _read_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default if default is not None else {}
    return default if default is not None else {}


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_id() -> str:
    return uuid.uuid4().hex[:10]


# ============ Domain logic ============
def load_winners() -> list[dict]:
    """For each canonical item, return the cheaper vendor + price.
    Items with no vendor pricing are still returned with `available=False`
    so the template can either hide them or show them as "always_show"."""
    canonical = _read_json(CANONICAL_FILE).get("items", [])
    alv_doc = _read_json(ALVARADO_FILE, {})
    jlu_doc = _read_json(JLUNA_FILE, {})
    alv = alv_doc.get("items", []) or []
    jlu = jlu_doc.get("items", []) or []
    # Per-vendor effective-date label (Sam 2026-05-11: surface vendor's
    # 'good from X to Y' on each row so users see when each price expires).
    alv_dr = _format_date_range(alv_doc.get("date_range"))
    jlu_dr = _format_date_range(jlu_doc.get("date_range"))

    def index_by_canonical(items):
        # Multiple vendor items can map to the same canonical key (e.g. JLUNA Cabbage Grn.CTN
        # and Cabbage Grn.SX both map to canonical "Cabbage Grn." 50lb). Keep the cheapest.
        idx = {}
        for it in items:
            key = (it.get("canonical_name", "").strip(), it.get("canonical_size", "").strip())
            if not key[0]:
                continue
            price = it.get("price")
            if price is None:
                continue
            existing = idx.get(key)
            if existing is None or price < existing.get("price", float("inf")):
                idx[key] = it
        return idx

    alv_idx = index_by_canonical(alv)
    jlu_idx = index_by_canonical(jlu)

    out = []
    for c in canonical:
        key = (c["name"], c.get("size", ""))
        a = alv_idx.get(key)
        j = jlu_idx.get(key)
        a_price = a.get("price") if a else None
        j_price = j.get("price") if j else None

        winner = None
        winner_price = None
        winner_vendor_name = None
        winner_vendor_size = None
        if a_price is not None and j_price is not None:
            if a_price <= j_price:
                winner, winner_price = "alvarado", a_price
                winner_vendor_name, winner_vendor_size = a.get("vendor_name", ""), a.get("vendor_size", "")
            else:
                winner, winner_price = "jluna", j_price
                winner_vendor_name, winner_vendor_size = j.get("vendor_name", ""), j.get("vendor_size", "")
        elif a_price is not None:
            winner, winner_price = "alvarado", a_price
            winner_vendor_name, winner_vendor_size = a.get("vendor_name", ""), a.get("vendor_size", "")
        elif j_price is not None:
            winner, winner_price = "jluna", j_price
            winner_vendor_name, winner_vendor_size = j.get("vendor_name", ""), j.get("vendor_size", "")

        always_show = bool(c.get("always_show", False))
        winner_dr = alv_dr if winner == "alvarado" else (jlu_dr if winner == "jluna" else None)
        out.append({
            "canonical_name": c["name"],
            "canonical_size": c.get("size", ""),
            "marker": c.get("marker"),
            "always_show": always_show,
            "vendor": winner,
            "vendor_label": "Alvarado" if winner == "alvarado" else ("J. Luna" if winner == "jluna" else None),
            "price": winner_price,
            "vendor_name": winner_vendor_name,
            "vendor_size": winner_vendor_size,
            "date_range": winner_dr,
            "available": winner is not None,
            "show_in_grid": (winner is not None) or always_show,
            "orderable": winner is not None,
        })
    return out


def _format_date_range(raw: str | None) -> str | None:
    """Convert vendor's raw date-range string into a compact 'M/D-M/D' label.

    Vendors send wildly different formats:
      - '05/10/2026-05/16/2026 REGULAR' (Alvarado)
      - 'MAY. 8 - 9, 2026' / 'MAY 10-16, 2026' (JLuna)
      - '5/10/2026 - 5/16/2026' (already-patched JLuna)
    Output: 'M/D - M/D' if we can extract two dates, else the trimmed raw
    string, else None. Keep month/day only — Sam reads it at a glance.
    """
    if not raw:
        return None
    import re
    s = raw.strip().upper()
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"SEPT":9,"OCT":10,"NOV":11,"DEC":12}
    # Numeric 'M/D/Y - M/D/Y' or 'M-D - M-D' style
    m = re.match(r"(\d{1,2})[/-](\d{1,2})(?:[/-]\d{2,4})?\s*-\s*(\d{1,2})[/-](\d{1,2})(?:[/-]\d{2,4})?", s)
    if m:
        return f"{int(m.group(1))}/{int(m.group(2))} - {int(m.group(3))}/{int(m.group(4))}"
    # Month-name style: 'MAY. 8 - 9, 2026' or 'MAY 10 - 16'
    m = re.match(r"([A-Z]{3,4})\.?\s*(\d{1,2})\s*-\s*(\d{1,2})", s)
    if m and m.group(1) in months:
        mo = months[m.group(1)]
        return f"{mo}/{int(m.group(2))} - {mo}/{int(m.group(3))}"
    # Cross-month: 'MAY 28 - JUN 3'
    m = re.match(r"([A-Z]{3,4})\.?\s*(\d{1,2})\s*-\s*([A-Z]{3,4})\.?\s*(\d{1,2})", s)
    if m and m.group(1) in months and m.group(3) in months:
        return f"{months[m.group(1)]}/{int(m.group(2))} - {months[m.group(3)]}/{int(m.group(4))}"
    # Fallback: trimmed raw, capped length
    return raw.strip()[:30]


def get_managers() -> dict:
    return _read_json(MANAGERS_FILE).get("managers", {})


def get_locations() -> dict:
    return _read_json(LOCATIONS_FILE).get("locations", {})


def get_vendor_routing() -> tuple[str, dict]:
    cfg = _read_json(VENDOR_ROUTING_FILE)
    mode = cfg.get("mode", "test")
    return mode, cfg.get(mode, {})


# ============ Email composition ============
def _format_date_long(yyyy_mm_dd: str) -> str:
    try:
        d = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d")
        return d.strftime("%A %m/%d/%Y")
    except Exception:
        return yyyy_mm_dd


def _format_date_short(yyyy_mm_dd: str) -> str:
    try:
        d = datetime.strptime(yyyy_mm_dd, "%Y-%m-%d")
        return d.strftime("%m/%d/%Y")
    except Exception:
        return yyyy_mm_dd


def compose_vendor_email(vendor_label, items, manager_name, manager_phone,
                         location_key, location_block, delivery_date):
    today = datetime.now().strftime("%m/%d/%Y")
    delivery_long = _format_date_long(delivery_date)
    location_upper = location_key.upper()

    lines = [
        f"{location_upper}-cenas kitchen",
        f"Date ordered: {today}",
        f"Ordered by: {manager_name} Cell:{manager_phone} (Txt If Possible)",
        f"Date Delivery Date: {delivery_long}",
        "",
        "",
        f"{'Item':<32}{'Size':<12}{'Price':<10}{'Qty':<12}",
    ]
    total = 0.0
    for it in items:
        name = (it.get("vendor_name") or it.get("canonical_name") or "").strip()
        size = (it.get("vendor_size") or it.get("canonical_size") or "").strip()
        price = it["price"]
        qty = int(it["qty"])
        lines.append(f"{name:<32}{size:<12}${price:<9.2f}{qty} cases")
        total += price * qty
    lines.append("")
    lines.append("")
    lines.append(location_block)
    plain = "\n".join(lines)

    rows_html = ""
    for it in items:
        v_name = (it.get("vendor_name") or it.get("canonical_name") or "").strip()
        v_size = (it.get("vendor_size") or it.get("canonical_size") or "").strip()
        rows_html += (
            f"<tr>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{v_name}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{v_size}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee;text-align:right'>${it['price']:.2f}</td>"
            f"<td style='padding:6px 12px;border-bottom:1px solid #eee'>{int(it['qty'])} cases</td>"
            f"</tr>"
        )
    html = (
        "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif;color:#222;font-size:14px;line-height:1.5'>"
        f"<div><strong>{location_upper}-cenas kitchen</strong></div>"
        f"<div>Date ordered: {today}</div>"
        f"<div>Ordered by: {manager_name} &nbsp;Cell:{manager_phone} (Txt If Possible)</div>"
        f"<div>Delivery Date: {delivery_long}</div>"
        "<br><br>"
        "<table style='border-collapse:collapse;font-size:14px'>"
        "<thead><tr>"
        "<th style='text-align:left;padding:6px 12px;border-bottom:2px solid #333'>Item</th>"
        "<th style='text-align:left;padding:6px 12px;border-bottom:2px solid #333'>Size</th>"
        "<th style='text-align:right;padding:6px 12px;border-bottom:2px solid #333'>Price</th>"
        "<th style='text-align:left;padding:6px 12px;border-bottom:2px solid #333'>Qty</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table><br><br>"
        f"<div style='white-space:pre-line'>{location_block}</div>"
        "</body></html>"
    )

    subject = (
        f"{location_upper} Cenas Kitchen Order - {today} - "
        f"delivery {_format_date_short(delivery_date)}"
    )
    return subject, plain, html, total


def compose_manager_confirmation(manager_name, location_key, delivery_date, vendor_orders):
    today = datetime.now().strftime("%m/%d/%Y")
    delivery_long = _format_date_long(delivery_date)
    subject = f"Order Confirmation - {today} - {location_key}"

    sections = []
    grand_total = 0.0
    for vo in vendor_orders:
        block_lines = [
            f"--- {vo['vendor_label']} ---",
            f"Sent to: {vo['to_addr']}",
            f"Sent at: {vo['sent_at_iso']}",
            f"{'Item':<28}{'Size':<10}{'Price':<10}{'Qty':<12}",
        ]
        for it in vo["items"]:
            block_lines.append(
                f"{it['canonical_name']:<28}{(it['canonical_size'] or ''):<10}"
                f"${it['price']:<9.2f}{int(it['qty'])} cases"
            )
        block_lines.append(f"Total due to {vo['vendor_label']}: ${vo['total']:.2f}")
        block_lines.append("")
        sections.append("\n".join(block_lines))
        grand_total += vo["total"]

    plain = "\n".join([
        f"Hi {manager_name},",
        "",
        f"Your produce order for {location_key} (delivery {delivery_long}) has been sent to "
        "the vendors. Have these check totals ready:",
        "",
        *sections,
        f"GRAND TOTAL across vendors: ${grand_total:.2f}",
        "",
        "If anything looks wrong, reply to this email or text Sam.",
        "",
        "— Cenas Kitchen automated order system",
    ])

    html_sections = ""
    for vo in vendor_orders:
        rows = ""
        for it in vo["items"]:
            rows += (
                f"<tr><td style='padding:5px 10px'>{it['canonical_name']}</td>"
                f"<td style='padding:5px 10px'>{it['canonical_size'] or ''}</td>"
                f"<td style='padding:5px 10px;text-align:right'>${it['price']:.2f}</td>"
                f"<td style='padding:5px 10px'>{int(it['qty'])} cases</td></tr>"
            )
        html_sections += (
            "<div style='margin-bottom:20px;padding:12px;background:#f7f7f7;border-radius:6px'>"
            f"<h3 style='margin:0 0 8px 0;color:#1a4f9a'>{vo['vendor_label']}</h3>"
            f"<div style='font-size:12px;color:#666'>Sent to: {vo['to_addr']} &nbsp;at&nbsp; {vo['sent_at_iso']}</div>"
            "<table style='border-collapse:collapse;width:100%;margin-top:8px;font-size:13px'>"
            "<thead><tr style='background:#e8e8e8'>"
            "<th style='text-align:left;padding:5px 10px'>Item</th>"
            "<th style='text-align:left;padding:5px 10px'>Size</th>"
            "<th style='text-align:right;padding:5px 10px'>Price</th>"
            "<th style='text-align:left;padding:5px 10px'>Qty</th>"
            f"</tr></thead><tbody>{rows}</tbody></table>"
            f"<div style='margin-top:8px;font-weight:bold'>Total due: ${vo['total']:.2f}</div>"
            "</div>"
        )

    html = (
        "<!DOCTYPE html><html><body style='font-family:Arial,sans-serif;color:#222;max-width:700px;margin:20px auto;padding:0 16px'>"
        f"<h2 style='color:#1a7a2e'>Order Confirmation — {location_key}</h2>"
        f"<p>Hi {manager_name}, your order for delivery <strong>{delivery_long}</strong> has been sent. "
        "Have these check totals ready:</p>"
        f"{html_sections}"
        "<div style='background:#1a7a2e;color:#fff;padding:14px 20px;border-radius:6px;font-size:18px;font-weight:bold;margin-top:16px'>"
        f"GRAND TOTAL across vendors: ${grand_total:.2f}</div>"
        "<p style='margin-top:20px;font-size:12px;color:#888'>If anything looks wrong, "
        "reply to this email or text Sam.</p></body></html>"
    )
    return subject, plain, html


# ============ SMTP send ============
def smtp_send(to_addr: str, subject: str, plain: str, html: str) -> None:
    pwd = _email_pwd()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"] = to_addr
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as srv:
        srv.login(SMTP_USER, pwd)
        srv.sendmail(SMTP_USER, [to_addr], msg.as_string())


# ============ Telegram ============
def telegram_send(text: str) -> tuple[bool, str | dict]:
    token = _tg_token()
    if not token:
        return False, "no telegram token"
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": SAM_TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode())
        return resp.get("ok", False), resp
    except Exception as e:
        return False, str(e)


# ============ Order processing ============
def execute_order(order: dict) -> dict:
    """Send vendor emails + manager confirmation. Update completed_orders.json."""
    by_vendor: dict[str, list[dict]] = {}
    for it in order["cart"]:
        by_vendor.setdefault(it["vendor"], []).append(it)

    mode, routing = get_vendor_routing()
    locations = get_locations()
    location = locations.get(order["used_location"])
    if not location:
        raise RuntimeError(f"Unknown location: {order['used_location']}")
    addr_block = location["address_block"]

    vendor_send_summaries = []
    errors = []

    for vendor_key, items in by_vendor.items():
        route = routing.get(vendor_key)
        if not route:
            errors.append(f"No routing for vendor {vendor_key}")
            continue
        to_addr = route["to"]
        vendor_label = route["label"]

        subject, plain, html, total = compose_vendor_email(
            vendor_label=vendor_label,
            items=items,
            manager_name=order["manager"],
            manager_phone=order["manager_phone"],
            location_key=order["used_location"],
            location_block=addr_block,
            delivery_date=order["delivery_date"],
        )
        sent_at = _now_iso()
        try:
            smtp_send(to_addr, subject, plain, html)
            vendor_send_summaries.append({
                "vendor": vendor_key, "vendor_label": vendor_label, "to_addr": to_addr,
                "items": items, "total": total, "sent_at_iso": sent_at, "ok": True,
            })
        except Exception as e:
            logger.exception("vendor send failed for %s", vendor_key)
            errors.append(f"Vendor {vendor_key} send failed: {e}")
            vendor_send_summaries.append({
                "vendor": vendor_key, "vendor_label": vendor_label, "to_addr": to_addr,
                "items": items, "total": total, "sent_at_iso": sent_at, "ok": False, "error": str(e),
            })

    confirm_sent_at = None
    if vendor_send_summaries:
        try:
            csubject, cplain, chtml = compose_manager_confirmation(
                manager_name=order["manager"],
                location_key=order["used_location"],
                delivery_date=order["delivery_date"],
                vendor_orders=vendor_send_summaries,
            )
            smtp_send(order["manager_email"], csubject, cplain, chtml)
            confirm_sent_at = _now_iso()
        except Exception as e:
            logger.exception("manager confirmation send failed")
            errors.append(f"Manager confirmation send failed: {e}")

    completed = _read_json(COMPLETED_ORDERS_FILE, {})
    completed[order["order_id"]] = {
        **order,
        "executed_at": _now_iso(),
        "vendor_sends": vendor_send_summaries,
        "manager_confirmation_at": confirm_sent_at,
        "errors": errors,
        "test_mode": mode == "test",
    }
    _write_json(COMPLETED_ORDERS_FILE, completed)

    return {
        "vendor_sends": vendor_send_summaries,
        "errors": errors,
        "manager_confirmation_at": confirm_sent_at,
    }


# ============ Routes ============
@produce_order.route("/")
def index():
    items = load_winners()
    grid_items = [i for i in items if i["show_in_grid"]]
    unavailable = [i for i in items if not i["show_in_grid"]]
    mode, _ = get_vendor_routing()
    return render_template(
        "produce/order_guide.html",
        items=grid_items,
        unavailable=unavailable,
        managers=get_managers(),
        locations=get_locations(),
        test_mode=(mode == "test"),
    )


@produce_order.route("/submit", methods=["POST"])
def submit():
    data = request.get_json(silent=True) or {}
    manager = (data.get("manager") or "").strip()
    location = (data.get("location") or "").strip()
    delivery_date = (data.get("delivery_date") or "").strip()
    cart_in = data.get("cart") or []

    mgrs = get_managers()
    locs = get_locations()
    if manager not in mgrs:
        return jsonify({"ok": False, "error": f"Unknown manager: {manager}"}), 400
    if location not in locs:
        return jsonify({"ok": False, "error": f"Unknown location: {location}"}), 400
    if not delivery_date:
        return jsonify({"ok": False, "error": "Delivery date required"}), 400

    winners = {(w["canonical_name"], w["canonical_size"]): w for w in load_winners() if w["orderable"]}
    cart = []
    for c in cart_in:
        key = (c.get("canonical_name", ""), c.get("canonical_size", ""))
        try:
            qty_int = int(c.get("qty", 0))
        except (ValueError, TypeError):
            qty_int = 0
        if qty_int <= 0:
            continue
        w = winners.get(key)
        if not w:
            continue
        cart.append({
            "canonical_name": w["canonical_name"],
            "canonical_size": w["canonical_size"],
            "vendor": w["vendor"],
            "vendor_name": w["vendor_name"],
            "vendor_size": w["vendor_size"],
            "price": w["price"],
            "qty": qty_int,
        })

    if not cart:
        return jsonify({"ok": False, "error": "Cart is empty"}), 400

    mgr_info = mgrs[manager]
    default_location = mgr_info.get("default_location")
    mismatch = (default_location is not None and default_location != location)

    order = {
        "order_id": _short_id(),
        "submitted_at": _now_iso(),
        "manager": manager,
        "manager_email": mgr_info["email"],
        "manager_phone": mgr_info["phone"],
        "default_location": default_location,
        "selected_location": location,
        "used_location": location,
        "delivery_date": delivery_date,
        "cart": cart,
    }

    if mismatch:
        pending = _read_json(PENDING_ORDERS_FILE, {})
        order["status"] = "pending_verification"
        pending[order["order_id"]] = order
        _write_json(PENDING_ORDERS_FILE, pending)

        item_count = len(cart)
        grand_total = sum(c["price"] * c["qty"] for c in cart)

        approve_url = url_for("produce_order.confirm",
                              order_id=order["order_id"], location=location, _external=True)
        override_url = url_for("produce_order.confirm",
                               order_id=order["order_id"], location=default_location, _external=True)
        cancel_url = url_for("produce_order.cancel", order_id=order["order_id"], _external=True)

        msg = (
            f"⚠️ Produce order verification needed\n"
            f"Manager: {manager} (default: {default_location or 'none'})\n"
            f"Selected: {location}\n"
            f"Items: {item_count}, est total: ${grand_total:.2f}\n"
            f"Delivery: {_format_date_long(delivery_date)}\n\n"
            f"Tap to confirm:\n"
            f"→ Approve as {location}: {approve_url}\n"
            f"→ Override to {default_location}: {override_url}\n"
            f"→ Cancel: {cancel_url}"
        )
        ok, _ = telegram_send(msg)
        return jsonify({
            "ok": True,
            "status": "held",
            "order_id": order["order_id"],
            "message": "Order held for verification. Sam has been notified. "
                       "You'll receive a confirmation email when released.",
            "telegram_sent": ok,
        })

    order["status"] = "executing"
    result = execute_order(order)
    if result["errors"]:
        return jsonify({
            "ok": False,
            "status": "errored",
            "order_id": order["order_id"],
            "errors": result["errors"],
            "vendor_sends": result["vendor_sends"],
        })
    return jsonify({
        "ok": True,
        "status": "sent",
        "order_id": order["order_id"],
        "vendor_sends": [
            {"vendor_label": v["vendor_label"], "to_addr": v["to_addr"], "total": v["total"]}
            for v in result["vendor_sends"]
        ],
        "manager_confirmation_at": result["manager_confirmation_at"],
    })


@produce_order.route("/confirm/<order_id>/<location>")
def confirm(order_id: str, location: str):
    pending = _read_json(PENDING_ORDERS_FILE, {})
    if order_id not in pending:
        return render_template(
            "produce/confirmed.html",
            error=f"Order {order_id} not found in pending (already processed?)",
            order_id=order_id,
        ), 404
    order = pending.pop(order_id)
    locs = get_locations()
    if location not in locs:
        return render_template(
            "produce/confirmed.html",
            error=f"Unknown location: {location}",
            order_id=order_id,
        ), 400

    order["used_location"] = location
    order["status"] = "executing"
    order["sam_confirmed_at"] = _now_iso()
    order["sam_confirmed_location"] = location

    _write_json(PENDING_ORDERS_FILE, pending)

    result = execute_order(order)
    return render_template("produce/confirmed.html", order=order, result=result, order_id=order_id)


@produce_order.route("/cancel/<order_id>")
def cancel(order_id: str):
    pending = _read_json(PENDING_ORDERS_FILE, {})
    if order_id not in pending:
        return render_template(
            "produce/canceled.html",
            error=f"Order {order_id} not found",
            order_id=order_id,
        ), 404
    order = pending.pop(order_id)
    order["status"] = "canceled"
    order["canceled_at"] = _now_iso()
    _write_json(PENDING_ORDERS_FILE, pending)

    completed = _read_json(COMPLETED_ORDERS_FILE, {})
    completed[order_id] = order
    _write_json(COMPLETED_ORDERS_FILE, completed)

    try:
        smtp_send(
            order["manager_email"],
            f"Order Canceled - {order_id}",
            f"Hi {order['manager']},\n\nYour produce order ({order_id}) was canceled by Sam.\n\n"
            "If you think this was a mistake, please text Sam.",
            f"<p>Hi {order['manager']},</p>"
            f"<p>Your produce order (<code>{order_id}</code>) was canceled by Sam.</p>"
            "<p>If you think this was a mistake, please text Sam.</p>",
        )
    except Exception:
        logger.exception("manager cancellation notification send failed")

    return render_template("produce/canceled.html", order=order, order_id=order_id)


@produce_order.route("/healthz")
def healthz():
    return jsonify({
        "ok": True,
        "ts": _now_iso(),
        "items_loaded": len(load_winners()),
        "managers_loaded": len(get_managers()),
        "locations_loaded": len(get_locations()),
        "pending_orders": len(_read_json(PENDING_ORDERS_FILE, {})),
        "alvarado_priced_at": _read_json(ALVARADO_FILE, {}).get("parsed_at"),
        "jluna_priced_at": _read_json(JLUNA_FILE, {}).get("parsed_at"),
    })


@produce_order.route("/admin/ingest-state")
def ingest_state():
    """Diagnostic: dump produce_ingest state file + approved_senders.
    Lightly gated by INGEST_TOKEN bearer header so only ck/aick can hit it.
    Transient — remove once produce-ingest debug is over."""
    from flask import request
    expected = (os.getenv("INGEST_TOKEN") or "").strip()
    auth = (request.headers.get("Authorization") or "").strip()
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if not expected or token != expected:
        return jsonify({"error": "unauthorized"}), 401
    state_dir = Path(os.getenv("PRODUCE_STATE_DIR") or (REPO_ROOT / "instance" / "produce"))
    state_path = state_dir / "ingest_state.json"
    config_dir = Path(os.getenv("PRODUCE_CONFIG_DIR") or (REPO_ROOT / "data" / "produce"))
    approved_path = config_dir / "approved_senders.json"
    out = {
        "state_path": str(state_path),
        "state_exists": state_path.exists(),
        "state": _read_json(state_path, {}) if state_path.exists() else None,
        "approved_senders_path": str(approved_path),
        "approved_senders_exists": approved_path.exists(),
        "approved_senders": _read_json(approved_path, {}) if approved_path.exists() else None,
    }
    # Optional: dump a vendor json (alvarado/jluna) or canonical_items
    if request.args.get("dump_vendor"):
        v = request.args["dump_vendor"]
        if v == "canonical":
            cp = config_dir / "canonical_items.json"
            out["canonical_items"] = _read_json(cp, {}) if cp.exists() else None
        else:
            vp = state_dir / f"{v}.json"
            out[f"{v}.json"] = _read_json(vp, {}) if vp.exists() else None
    if request.args.get("reset_last_seen"):
        try:
            new_val = int(request.args.get("reset_last_seen"))
            state = _read_json(state_path, {"last_seen_mid": 0, "processed": {}})
            state["last_seen_mid"] = new_val
            # Also clear the processed dict so seen-but-skipped mids get re-evaluated.
            if request.args.get("clear_processed") == "1":
                state["processed"] = {}
            (state_dir).mkdir(parents=True, exist_ok=True)
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(state_path)
            out["reset_to"] = new_val
            out["state_after"] = state
        except Exception as e:
            out["reset_error"] = str(e)
    # Optional: patch a vendor's date_range string (Sam's manual override per
    # 2026-05-11 "change 8 to 9 to 10 to 16, just for this time" — JLuna sent
    # an old 5/8-5/9 list but the prices are current for 5/10-5/16).
    if request.args.get("patch_vendor"):
        try:
            vendor = request.args["patch_vendor"]
            new_dr = request.args.get("set_date_range")
            vendor_path = state_dir / f"{vendor}.json"
            data = _read_json(vendor_path, {})
            if not data:
                out["patch_error"] = f"{vendor}.json not found"
            elif not new_dr:
                out["patch_error"] = "set_date_range param missing"
            else:
                old_dr = data.get("date_range")
                data["date_range"] = new_dr
                tmp = vendor_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp.replace(vendor_path)
                out["patch_applied"] = {"vendor": vendor, "old": old_dr, "new": new_dr}
        except Exception as e:
            out["patch_error"] = str(e)
    return jsonify(out)
