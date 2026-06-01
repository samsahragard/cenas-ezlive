"""Schedules V2 - Block 8: the employee availability PAGE (frontend shell, ck).

GET /employee/availability renders the mobile view where a logged-in employee
declares their recurring weekly availability (the windows they CAN work) and adds
one-off unavailability blocks (CANNOT-work spans).

Same split as B5/B6/B7: this page route attaches to the employee_auth blueprint
(imported in app/__init__.py before ezempauth.install) so employee_auth.py stays
ckai's lane. The page reads ONLY the Employee name; ckai owns the availability
models + the data/action endpoints. Config hands the client the endpoint PATHS,
LOCKED #1986 (B7-style): the PAGE owns the parent /employee/availability, the DATA
list is at /employee/availability/list.

AUTH: /employee/availability is in auth.py EXEMPT_PREFIXES so a session-less hit
reaches THIS view (not the staff keypad); the view self-guards on
session['employee_id'] and 302s to /employee/login. The one prefix also covers
ckai's /employee/availability/list + /recurring + /block endpoints, each with its
own employee_id guard (401 JSON). Every datum here is scoped to session['employee_id'].
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from flask import redirect, render_template, session

from app.db import SessionLocal
from app.models import Employee
from app.web.employee_auth import employee_auth


@employee_auth.route("/employee/availability", methods=["GET"])
def employee_availability_page():
    """RETIRED (D2): availability is now manager-controlled, so the employee
    self-service editor is gone. The route stays REGISTERED (no 404) but simply
    bounces to the employee dashboard instead of rendering the old editor."""
    return redirect("/employee/dashboard")
