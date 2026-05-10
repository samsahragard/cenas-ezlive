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

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, create_engine
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


_engine = None
_Session = None


def _get_db_url() -> str | None:
    return os.environ.get("CORPORATE_DB_URL") or None


def is_configured() -> bool:
    return bool(_get_db_url())


def _ensure_engine():
    global _engine, _Session
    if _engine is not None:
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


@contextmanager
def session():
    """Yield a session bound to cenas_db."""
    _ensure_engine()
    s = _Session()
    try:
        yield s
    finally:
        s.close()


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
        q = s.query(Product).order_by(Product.category.asc(), Product.product_name.asc())
        if category:
            q = q.filter(Product.category == category)
        rows = q.all()
        return [{
            "id": p.id,
            "name": p.product_name,
            "in_stock": p.in_stock,
            "picture": p.product_picture,
            "category": p.category,
            "date_added": p.date_added,
        } for p in rows]


def list_categories() -> list[str]:
    """Distinct categories in the catalog (sorted)."""
    _ensure_engine()
    with session() as s:
        rows = s.query(Product.category).distinct().order_by(Product.category.asc()).all()
        return [r[0] for r in rows if r[0]]


def list_orders(limit: int = 50, store_filter: str | None = None) -> list[dict]:
    """Recent orders. store_filter narrows to a store's synthetic Customer."""
    _ensure_engine()
    with session() as s:
        q = s.query(Order).order_by(Order.submitted_at.desc()).limit(limit)
        if store_filter:
            email = STORE_CUSTOMER_EMAIL.get(store_filter)
            if email:
                cust = s.query(Customer).filter_by(email=email).one_or_none()
                if cust:
                    q = q.filter(Order.customer_link == cust.id)
                else:
                    return []
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
                    "name": it.product_name,
                    "category": it.product_category,
                    "quantity": it.quantity,
                } for it in (o.items or [])],
                "total_quantity": sum((it.quantity or 0) for it in (o.items or [])),
            })
        return out


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
            oi = OrderItem(
                order_link=order.id,
                product_name=p.product_name,
                product_category=p.category,
                quantity=qty,
            )
            s.add(oi)
            # Decrement stock — clamp at 0 so we never go negative.
            p.in_stock = max(0, (p.in_stock or 0) - qty)
            line_dicts.append({
                "name": p.product_name,
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
