from __future__ import annotations

from datetime import datetime

from flask import Blueprint, render_template

from app.db import get_db
from app.models import Order, OrderItem

review = Blueprint("review", __name__)

@review.route("/review")
def review_queue():
    db = next(get_db())
    try:
        # Rolling window: only show today's + upcoming orders. Orders with
        # delivery_date in the past are archived from the review queue.
        today_iso = datetime.now().strftime("%Y-%m-%d")
        orders = (
            db.query(Order)
            .filter(Order.delivery_date >= today_iso)
            .order_by(Order.delivery_date.asc(), Order.deliver_at)
            .all()
        )
        return render_template("review_queue.html", orders=orders)
    finally:
        db.close()

@review.route("/review/<external_order_id>")
def review_details(external_order_id: str):
    db = next(get_db())
    try:
        order = db.query(Order).filter_by(external_order_id=external_order_id).first()
        if not order:
            return render_template("review_queue.html", orders=[], error=f"Order {external_order_id!r} not found.")
    
        items = (
            db.query(OrderItem)
            .filter_by(order_id=order.id)
            .all()
        )
        return render_template("review_detail.html", order=order, items=items)
    finally:
        db.close()
