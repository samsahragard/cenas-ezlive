"""Schedules V2 - Block 11 (email-onboarding swap): the employee SELF-SETUP page (frontend, ck).

GET /employee/setup/<token> renders the mobile page a new hire reaches from the
one-time setup link emailed to them after a manager admin-adds them (name +
email). There they complete their personal info and CREATE their login by
setting a 5-digit passcode.

Same house split as B5-B9 (employee_*_page.py): this page route lives in its own
file and ATTACHES to the existing employee_auth blueprint (imported for its
decorator side effect in app/__init__.py BEFORE ezempauth.install registers the
blueprint), so employee_auth.py stays in ckai's lane and all /employee/* routes
share one namespace.

AUTH: unlike the other /employee/* pages this one is PRE-login - the new hire has
NO session yet; the TOKEN is the credential. So this route does NOT guard on
session["employee_id"]; it just renders the shell (like login_page). The path is
added to auth.py EXEMPT_PREFIXES so the emailed link is reachable anonymously.

ckai owns the token-scoped endpoints this page calls (contract #2103/#2107):
  GET  /employee/setup/<token>/info      -> {ok, valid, employee:{full_name, email}}
  POST /employee/setup/<token>/complete  {full_name, phone?, passcode} -> 200 {ok} | 410
Single-use token consumption, expiry, and the no-IDOR / token-scoping invariants
are enforced THERE (backend). This page only carries the token + collects the
fields. Profile fields (samai #2107): full_name (prefilled, editable), email
(read-only from invite), phone (optional, records-only), passcode (5-digit).
Store + position(s) are MANAGER-set elsewhere - never on this page.
"""
from __future__ import annotations

import json

from flask import render_template

from app.web.employee_auth import employee_auth


@employee_auth.route("/employee/setup/<token>", methods=["GET"])
def employee_setup_page(token):
    """Render the client-side self-setup view. Anonymous-reachable (the token is
    the credential; no employee session yet). On load the page fetches infoUrl to
    validate the token + prefill name/email; an invalid/expired/used token (info
    valid:false, or the complete POST returns 410) shows a 'link expired' state
    instead of the form. The token is relayed into the child endpoint paths; ckai
    validates + consumes it server-side."""
    config = {
        "infoUrl": "/employee/setup/%s/info" % token,
        "completeUrl": "/employee/setup/%s/complete" % token,
        "loginUrl": "/employee/login",
        "passcodeLen": 5,
    }
    return render_template(
        "employee_setup.html",
        config_json=json.dumps(config),
        login_url="/employee/login",
    )
