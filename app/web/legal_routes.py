"""Public legal pages — privacy policy and terms.

These pages are intentionally NOT behind the site-password / keypad gate
so that Google Play (and any other auditor) can crawl + verify them
when reviewing the Cenas Kitchen Employee app's listing. The /privacy
URL goes in the Play Console's 'Privacy Policy' field.
"""
from __future__ import annotations

from flask import Blueprint, render_template

legal = Blueprint("legal", __name__)


@legal.route("/privacy")
@legal.route("/privacy/")
def privacy():
    return render_template("privacy.html")
