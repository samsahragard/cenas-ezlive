"""Review Queue — RETIRED 2026-05-10.

Replaced by the auto-resolver pipeline (see ezcater_webhook + orders_service):
when an order arrives with extraction warnings, the system re-pulls from the
ezCater Partner API, re-validates, and Telegrams Sam if anything is still off.
No more manual queue page.

Old `/review` and `/review/<id>` URLs now redirect to the store-picker so
existing bookmarks don't 404.
"""
from __future__ import annotations

from flask import Blueprint, redirect

review = Blueprint("review", __name__)


@review.route("/review")
@review.route("/review/<external_order_id>")
def review_redirect(external_order_id: str | None = None):
    return redirect("/")
