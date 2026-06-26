"""Corporate Order shop layer — read/write to the marketing-site Postgres
(`cenas_db`) so the catalog + inventory shown on app.cenaskitchen.com stays
in sync with the public shop on cenaskitchen.com.

DB connection comes from the CORPORATE_DB_URL env var. The tables are owned
by the cenas_website Flask app (see github.com/leverr18/cenas_website); we
just mirror the model classes here for read + write access. If the schema
on the cenas_website side changes, mirror it here.

Per-store ordering pattern: each store (Tomball, Copperfield) is represented
by a synthetic Customer row in cenas_db so Order.customer_link FK stays
valid. The store-customers are created lazily on first call.
"""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, create_engine,
    case, func, inspect, text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

log = logging.getLogger(__name__)

CorporateBase = declarative_base()

# Synthetic Customer emails used to attribute store-placed orders. Picking
# real-looking emails so they're recognizable in cenas_website's order
# history if anyone browses there.
STORE_CUSTOMER_EMAIL = {
    "tomball":     "store-tomball@cenaskitchen.com",
    "copperfield": "store-copperfield@cenaskitchen.com",
    "corporate":   "corporate@cenaskitchen.com",
    "partner":     "partner@cenaskitchen.com",
}
STORE_CUSTOMER_NAME = {
    "tomball":     "Tomball Kitchen",
    "copperfield": "Copperfield Kitchen",
    "corporate":   "Corporate Office",
    "partner":     "Partners",
}


class Customer(CorporateBase):
    __tablename__ = "customer"
    id = Column(Integer, primary_key=True)
    email = Column(String(100), unique=True, nullable=False)
    username = Column(String(100), nullable=False)
    password_hash = Column(String(150))
    date_joined = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Product(CorporateBase):
    __tablename__ = "product"
    id = Column(Integer, primary_key=True)
    product_name = Column(String(100), nullable=False)
    in_stock = Column(Integer, nullable=False)
    product_picture = Column(String(1000), nullable=False)
    category = Column(String(100), nullable=False)
    sort_order = Column(Integer)
    date_added = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Order(CorporateBase):
    __tablename__ = "order"
    id = Column(Integer, primary_key=True)
    submitted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    status = Column(String(100), nullable=False, default="Submitted")
    customer_link = Column(Integer, ForeignKey("customer.id"), nullable=False)

    items = relationship("OrderItem", backref="order", cascade="all, delete-orphan")
    customer = relationship("Customer")


class OrderItem(CorporateBase):
    __tablename__ = "order_item"
    id = Column(Integer, primary_key=True)
    order_link = Column(Integer, ForeignKey("order.id"), nullable=False)
    product_name = Column(String(100), nullable=False)
    product_category = Column(String(100), nullable=False)
    quantity = Column(Integer, nullable=False)
    fulfilled_quantity = Column(Integer)


_engine = None
_Session = None
_catalog_checked = False
_schema_checked = False

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "corporate_order_catalog.json"
_MEDIA_BASE_URL = (os.environ.get("CORPORATE_MEDIA_BASE_URL") or "https://cenaskitchen.com/media").rstrip("/")


def _get_db_url() -> str | None:
    return os.environ.get("CORPORATE_DB_URL") or None


def is_configured() -> bool:
    return bool(_get_db_url())


def _ensure_engine():
    global _engine, _Session
    if _engine is not None:
        _ensure_schema()
        return
    url = _get_db_url()
    if not url:
        raise RuntimeError(
            "CORPORATE_DB_URL not set — set it on Render to the cenas_db "
            "Postgres connection string."
        )
    # Render's internal Postgres URLs use 'postgresql://' which SQLAlchemy
    # accepts as the legacy driver name. Force psycopg (v3) explicitly so
    # we avoid pulling in psycopg2 which isn't in requirements.
    if url.startswith("postgresql://") and "+psycopg" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    _engine = create_engine(url, pool_pre_ping=True, pool_recycle=300, future=True)
    _Session = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False, future=True)
    _ensure_schema()


def _ensure_schema() -> None:
    """Apply small backwards-compatible columns this app needs on cenas_db."""
    global _schema_checked
    if _schema_checked or _engine is None:
        return
    insp = inspect(_engine)
    try:
        order_item_cols = {c["name"] for c in insp.get_columns("order_item")}
    except Exception:
        order_item_cols = set()
    if "fulfilled_quantity" not in order_item_cols:
        with _engine.begin() as conn:
            conn.execute(text("ALTER TABLE order_item ADD COLUMN fulfilled_quantity INTEGER"))
    try:
        product_cols = {c["name"] for c in insp.get_columns("product")}
    except Exception:
        product_cols = set()
    if "sort_order" not in product_cols:
        with _engine.begin() as conn:
            conn.execute(text("ALTER TABLE product ADD COLUMN sort_order INTEGER"))
    _backfill_sort_order()
    _schema_checked = True


@contextmanager
def session():
    """Yield a session bound to cenas_db."""
    _ensure_engine()
    s = _Session()
    try:
        yield s
    finally:
        s.close()


def _clean_product_name(name: str | None) -> str:
    """Normalize legacy product display whitespace without changing IDs."""
    return re.sub(r"\s+", " ", name or "").strip()


def _name_key(name: str | None) -> str:
    return _clean_product_name(name).casefold()


def _picture_url(picture: str | None) -> str:
    raw = (picture or "").strip()
    if not raw:
        return ""
    if raw.startswith(("http://", "https://", "data:")):
        return raw
    if raw.startswith("/media/"):
        return "https://cenaskitchen.com" + quote(raw, safe="/:%?=&%")
    if raw.startswith("/"):
        return "https://cenaskitchen.com" + quote(raw, safe="/:%?=&%")
    return f"{_MEDIA_BASE_URL}/{quote(raw, safe='-_.~%')}"


def _product_ordering():
    return (
        Product.category.asc(),
        case((Product.sort_order.is_(None), 1), else_=0).asc(),
        Product.sort_order.asc(),
        Product.product_name.asc(),
        Product.id.asc(),
    )


def _category_ordering():
    return (
        case((Product.sort_order.is_(None), 1), else_=0).asc(),
        Product.sort_order.asc(),
        Product.product_name.asc(),
        Product.id.asc(),
    )


def _backfill_sort_order() -> int:
    """Give existing rows a stable initial order without disturbing custom order."""
    if _engine is None:
        return 0
    with _engine.begin() as conn:
        rows = conn.execute(text(
            """
            SELECT id, category, product_name, sort_order
            FROM product
            ORDER BY category ASC,
                     CASE WHEN sort_order IS NULL THEN 1 ELSE 0 END ASC,
                     sort_order ASC,
                     product_name ASC,
                     id ASC
            """
        )).mappings().all()
        by_category: dict[str, list[dict]] = {}
        for row in rows:
            by_category.setdefault(row["category"] or "", []).append(row)
        updated = 0
        for category_rows in by_category.values():
            for idx, row in enumerate(category_rows, start=1):
                if row["sort_order"] is not None:
                    continue
                conn.execute(
                    text("UPDATE product SET sort_order = :sort_order WHERE id = :id"),
                    {"sort_order": idx * 10, "id": row["id"]},
                )
                updated += 1
        return updated


def _normalize_category_sort_orders(s, category: str) -> list[Product]:
    rows = (
        s.query(Product)
        .filter(Product.category == category)
        .order_by(*_category_ordering())
        .all()
    )
    for idx, product in enumerate(rows, start=1):
        wanted = idx * 10
        if product.sort_order != wanted:
            product.sort_order = wanted
    return rows


def _next_sort_order(s, category: str) -> int:
    max_order = (
        s.query(func.max(Product.sort_order))
        .filter(Product.category == category)
        .scalar()
    )
    return int(max_order or 0) + 10


def load_catalog_seed() -> dict:
    with _CATALOG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def sync_catalog_from_seed(
    *,
    update_existing_stock: bool = False,
    normalize_existing_names: bool = False,
) -> dict:
    """Insert missing corporate-order products from the repo catalog.

    The live product table predates this app and a few names carry cosmetic
    whitespace. Matching is therefore on normalized display name; by default
    existing rows keep their stock count and raw product_name.
    """
    _ensure_engine()
    seed = load_catalog_seed()
    items = seed.get("items") or []
    added = updated_stock = normalized_names = updated_category = 0
    with session() as s:
        existing = {
            _name_key(p.product_name): p
            for p in s.query(Product).all()
        }
        for item in items:
            name = _clean_product_name(item.get("name"))
            if not name:
                continue
            category = item.get("category") or item.get("category_label") or "Supplies"
            stock = int(item.get("in_stock") or 0)
            product = existing.get(_name_key(name))
            if product is None:
                product = Product(
                    product_name=name,
                    in_stock=max(0, stock),
                    product_picture=item.get("picture") or "",
                    category=category,
                    sort_order=_next_sort_order(s, category),
                )
                s.add(product)
                s.flush()
                existing[_name_key(name)] = product
                added += 1
                continue
            if normalize_existing_names and product.product_name != name:
                product.product_name = name
                normalized_names += 1
            if product.category != category:
                product.category = category
                updated_category += 1
            if update_existing_stock and product.in_stock != stock:
                product.in_stock = max(0, stock)
                updated_stock += 1
        if added or updated_stock or normalized_names or updated_category:
            s.commit()
    return {
        "seed_items": len(items),
        "added": added,
        "updated_stock": updated_stock,
        "normalized_names": normalized_names,
        "updated_category": updated_category,
    }


def ensure_catalog_seeded() -> dict:
    """Best-effort boot/request guard: insert missing rows once per process."""
    global _catalog_checked
    if _catalog_checked:
        return {"already_checked": True}
    result = sync_catalog_from_seed()
    _catalog_checked = True
    return result


def _ensure_store_customer(s, store_key: str) -> Customer:
    """Find-or-create the synthetic Customer row representing a store."""
    email = STORE_CUSTOMER_EMAIL[store_key]
    cust = s.query(Customer).filter_by(email=email).one_or_none()
    if cust:
        return cust
    cust = Customer(
        email=email,
        username=STORE_CUSTOMER_NAME[store_key],
        # No password_hash → these accounts can't log in to the public site
        password_hash=None,
    )
    s.add(cust)
    s.flush()
    log.info("corporate_shop: created store customer %s", email)
    return cust


def list_products(category: str | None = None) -> list[dict]:
    """Catalog query — returns a render-friendly dict per product."""
    _ensure_engine()
    with session() as s:
        q = s.query(Product).order_by(*_product_ordering())
        if category:
            q = q.filter(Product.category == category)
        rows = q.all()
        return [{
            "id": p.id,
            "name": _clean_product_name(p.product_name),
            "in_stock": p.in_stock,
            "picture": p.product_picture,
            "picture_url": _picture_url(p.product_picture),
            "category": p.category,
            "sort_order": p.sort_order,
            "date_added": p.date_added,
        } for p in rows]


def list_categories() -> list[str]:
    """Distinct categories in the catalog (sorted)."""
    _ensure_engine()
    with session() as s:
        rows = s.query(Product.category).distinct().order_by(Product.category.asc()).all()
        return [r[0] for r in rows if r[0]]


def list_orders(limit: int | None = 50, store_filter: str | None = None) -> list[dict]:
    """Recent orders. store_filter narrows to a store's synthetic Customer."""
    _ensure_engine()
    with session() as s:
        q = s.query(Order).order_by(Order.submitted_at.desc())
        if store_filter:
            email = STORE_CUSTOMER_EMAIL.get(store_filter)
            if email:
                cust = s.query(Customer).filter_by(email=email).one_or_none()
                if cust:
                    q = q.filter(Order.customer_link == cust.id)
                else:
                    return []
        if limit:
            q = q.limit(limit)
        out = []
        for o in q.all():
            cust = o.customer
            store_key = None
            for k, e in STORE_CUSTOMER_EMAIL.items():
                if cust and cust.email == e:
                    store_key = k
                    break
            out.append({
                "id": o.id,
                "submitted_at": o.submitted_at,
                "status": o.status,
                "customer_email": cust.email if cust else None,
                "customer_username": cust.username if cust else None,
                "store_key": store_key,
                # Key name 'lines' (not 'items') to dodge Jinja's dict.items
                # method-vs-key collision when rendering with `o.items`.
                "lines": [{
                    "id": it.id,
                    "name": it.product_name,
                    "category": it.product_category,
                    "quantity": it.quantity,
                    "fulfilled_quantity": it.fulfilled_quantity or 0,
                    "remaining_quantity": max(
                        0, (it.quantity or 0) - (it.fulfilled_quantity or 0)
                    ),
                } for it in (o.items or [])],
                "total_quantity": sum((it.quantity or 0) for it in (o.items or [])),
                "total_fulfilled": sum(
                    (it.fulfilled_quantity or 0) for it in (o.items or [])
                ),
            })
        return out


def add_product(
    *,
    name: str,
    category: str,
    in_stock: int,
    picture: str = "",
) -> dict:
    """Corporate admin: add a catalog item if the name is not already present."""
    _ensure_engine()
    clean_name = _clean_product_name(name)
    clean_category = _clean_product_name(category) or "Supplies"
    if not clean_name:
        raise ValueError("product name is required")
    with session() as s:
        existing = {
            _name_key(row[0])
            for row in s.query(Product.product_name).all()
        }
        if _name_key(clean_name) in existing:
            raise ValueError(f"{clean_name} is already in the corporate catalog")
        product = Product(
            product_name=clean_name,
            in_stock=max(0, int(in_stock or 0)),
            product_picture=picture or "",
            category=clean_category,
            sort_order=_next_sort_order(s, clean_category),
        )
        s.add(product)
        s.commit()
        return {
            "id": product.id,
            "name": clean_name,
            "category": clean_category,
            "in_stock": product.in_stock,
        }


def update_product_order(category: str, product_ids: list[int]) -> int:
    """Corporate admin: persist the display order for one department."""
    _ensure_engine()
    clean_category = _clean_product_name(category)
    if not clean_category:
        raise ValueError("category is required")
    seen: set[int] = set()
    ordered_ids: list[int] = []
    for pid in product_ids:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid in seen:
            continue
        seen.add(pid)
        ordered_ids.append(pid)
    with session() as s:
        current = _normalize_category_sort_orders(s, clean_category)
        by_id = {p.id: p for p in current}
        new_order = [by_id[pid] for pid in ordered_ids if pid in by_id]
        new_order.extend(p for p in current if p.id not in seen)
        if not new_order:
            return 0
        for idx, product in enumerate(new_order, start=1):
            product.sort_order = idx * 10
        s.commit()
        return len(new_order)


def delete_product(product_id: int) -> bool:
    """Corporate admin: remove a product from the live ordering catalog."""
    _ensure_engine()
    with session() as s:
        product = s.query(Product).filter_by(id=product_id).one_or_none()
        if not product:
            return False
        s.delete(product)
        s.commit()
        return True


def place_order(store_key: str, items: list[tuple[int, int]]) -> dict:
    """Create an Order in cenas_db on behalf of the given store.

    items = [(product_id, qty), ...]. Returns a dict with order id + line items.
    Decrements Product.in_stock for each line.
    """
    _ensure_engine()
    if store_key not in STORE_CUSTOMER_EMAIL:
        raise ValueError(f"unknown store_key {store_key!r}")
    with session() as s:
        cust = _ensure_store_customer(s, store_key)
        order = Order(customer_link=cust.id, status="Submitted")
        s.add(order)
        s.flush()
        line_dicts = []
        for pid, qty in items:
            if qty <= 0:
                continue
            p = s.query(Product).filter_by(id=pid).one_or_none()
            if not p:
                continue
            if (p.in_stock or 0) <= 0:
                continue
            if qty > (p.in_stock or 0):
                raise ValueError(
                    f"{_clean_product_name(p.product_name)} only has {p.in_stock} available"
                )
            clean_name = _clean_product_name(p.product_name)
            oi = OrderItem(
                order_link=order.id,
                product_name=clean_name,
                product_category=p.category,
                quantity=qty,
            )
            s.add(oi)
            # Decrement stock — clamp at 0 so we never go negative.
            p.in_stock = max(0, (p.in_stock or 0) - qty)
            line_dicts.append({
                "name": clean_name,
                "category": p.category,
                "quantity": qty,
                "remaining_stock": p.in_stock,
            })
        if not line_dicts:
            s.rollback()
            raise ValueError("no valid items to order")
        s.commit()
        return {
            "order_id": order.id,
            "submitted_at": order.submitted_at,
            "store_key": store_key,
            "store_label": STORE_CUSTOMER_NAME[store_key],
            "items": line_dicts,
        }


def update_stock(product_id: int, new_in_stock: int) -> bool:
    """Admin write: set Product.in_stock. Returns True if the row existed."""
    _ensure_engine()
    if new_in_stock < 0:
        new_in_stock = 0
    with session() as s:
        p = s.query(Product).filter_by(id=product_id).one_or_none()
        if not p:
            return False
        p.in_stock = new_in_stock
        s.commit()
        return True


def update_order_status(order_id: int, new_status: str) -> bool:
    _ensure_engine()
    with session() as s:
        o = s.query(Order).filter_by(id=order_id).one_or_none()
        if not o:
            return False
        o.status = new_status
        s.commit()
        return True


def update_order_fulfillment(
    order_id: int,
    fulfilled_by_line: dict[int, int],
    *,
    new_status: str | None = None,
) -> bool:
    """Corporate admin: save actual sent counts per line and optional status."""
    _ensure_engine()
    with session() as s:
        order = s.query(Order).filter_by(id=order_id).one_or_none()
        if not order:
            return False
        for line in order.items or []:
            if line.id not in fulfilled_by_line:
                continue
            sent = fulfilled_by_line[line.id]
            if sent < 0:
                sent = 0
            if sent > (line.quantity or 0):
                sent = line.quantity or 0
            line.fulfilled_quantity = sent
        if new_status:
            order.status = new_status
        s.commit()
        return True
