"""Token-gated, one-time Toast link writer for the CK performance rollout.

This module is intentionally separate from the catering/driver cron surface.
It can only insert the 18 Stage-B audited links approved by Sam, and it does
not expose a general-purpose link, unlink, or profile-create endpoint.
"""
from __future__ import annotations

import os
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.models import CenaToastLink, Employee
from app.db import SessionLocal


perf_roster_link_bp = Blueprint("perf_roster_link", __name__)


def _presented_token() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return request.headers.get("X-Cron-Token") or request.args.get("token")


LINK18 = (
    {"cena_employee_id": 100, "store_key": "copperfield", "toast_id": "ec0d7f40-0f3f-4bdc-ba82-a45db722fe74", "toast_name": "Alejandra Valencia"},
    {"cena_employee_id": 12, "store_key": "tomball", "toast_id": "3ccc213e-d071-461c-bf7c-1485b25296bf", "toast_name": "Ali mohammad Rao"},
    {"cena_employee_id": 78, "store_key": "copperfield", "toast_id": "2a887f22-51d1-4906-bf5b-ce5ac10fd5ee", "toast_name": "Aniya Owens"},
    {"cena_employee_id": 8, "store_key": "copperfield", "toast_id": "2caef749-7d82-4fa5-8296-a128ca3973e0", "toast_name": "Carlos Ruiz"},
    {"cena_employee_id": 11, "store_key": "copperfield", "toast_id": "a05d60e6-afee-4948-8db7-45285f3e70f7", "toast_name": "Chad Alexander Reid"},
    {"cena_employee_id": 10, "store_key": "tomball", "toast_id": "7e8c3ffd-d6db-4d0e-803b-425e0db5873a", "toast_name": "Damon Greer"},
    {"cena_employee_id": 1, "store_key": "tomball", "toast_id": "4f9b5c04-9323-4a9a-bf6e-0698a15f2ce7", "toast_name": "Elijah Lemos"},
    {"cena_employee_id": 59, "store_key": "copperfield", "toast_id": "0b35c4fc-5094-41d0-b637-6740c97a5331", "toast_name": "Geidis Dailen Alarcon"},
    {"cena_employee_id": 7, "store_key": "copperfield", "toast_id": "d8f9c0fb-212f-4a5e-87b2-995463a35449", "toast_name": "Glenda Soto"},
    {"cena_employee_id": 9, "store_key": "tomball", "toast_id": "fbb10844-1175-4b1e-810b-7548e4856824", "toast_name": "Ismael Villa Sanchez"},
    {"cena_employee_id": 46, "store_key": "copperfield", "toast_id": "a572c6c0-fb24-4b21-a513-c47013610dff", "toast_name": "Kimberly Rivera"},
    {"cena_employee_id": 79, "store_key": "copperfield", "toast_id": "1228e1dc-4af0-47ae-99b5-697f98867308", "toast_name": "Kristal Castillo Garcia"},
    {"cena_employee_id": 97, "store_key": "tomball", "toast_id": "4043ca7b-0a9d-4ba5-8e55-9b8006fce398", "toast_name": "Martin Arredondo Arvizu"},
    {"cena_employee_id": 29, "store_key": "copperfield", "toast_id": "6076eb8e-fbb7-4166-95e7-13e4cb0bd8d7", "toast_name": "Nancy Calderon Pedroso"},
    {"cena_employee_id": 4, "store_key": "tomball", "toast_id": "1b6cc8aa-dd55-45fd-9221-db38b9f6d9f1", "toast_name": "Natalie Allen"},
    {"cena_employee_id": 5, "store_key": "copperfield", "toast_id": "6a439765-8f15-4733-9894-98173d995242", "toast_name": "Odalis Arenciba Arzola"},
    {"cena_employee_id": 58, "store_key": "copperfield", "toast_id": "98775cf2-46a1-4786-9762-d388a84a921b", "toast_name": "Rubi Lira"},
    {"cena_employee_id": 62, "store_key": "copperfield", "toast_id": "380193b5-e8b6-47c2-bfdc-3005ec7562c8", "toast_name": "Tahily Vazquez"},
)


@perf_roster_link_bp.route("/cron/perf-roster-link", methods=["POST"])
def perf_roster_link():
    expected = os.getenv("CRON_TOKEN")
    if not expected or _presented_token() != expected:
        return jsonify({"ok": False, "error": "forbidden"}), 403

    body = request.get_json(silent=True) or {}
    if (body.get("action") or "").strip() != "link":
        return jsonify({"ok": False, "error": "action must be link"}), 400

    dry_run = bool(body.get("dry_run"))
    db = SessionLocal()
    try:
        employees = {
            employee.id: employee.full_name
            for employee in (
                db.query(Employee)
                .filter(
                    Employee.active.is_(True),
                    Employee.id.in_([row["cena_employee_id"] for row in LINK18]),
                )
                .all()
            )
        }
        missing = [
            {"cena_employee_id": row["cena_employee_id"], "store_key": row["store_key"]}
            for row in LINK18
            if row["cena_employee_id"] not in employees
        ]
        if missing:
            return jsonify({
                "ok": False,
                "error": "missing or inactive employees",
                "missing": missing,
            }), 409

        written = []
        skipped_same = []
        conflicts = []
        for row in LINK18:
            existing = (
                db.query(CenaToastLink)
                .filter_by(
                    cena_employee_id=row["cena_employee_id"],
                    store_key=row["store_key"],
                )
                .first()
            )
            public_row = {
                "cena_employee_id": row["cena_employee_id"],
                "employee_name": employees[row["cena_employee_id"]],
                "store_key": row["store_key"],
                "toast_name": row["toast_name"],
            }
            if existing is None:
                written.append(public_row)
                continue
            if existing.toast_id == row["toast_id"] and existing.toast_name == row["toast_name"]:
                skipped_same.append({**public_row, "reason": "already_linked_to_audited_toast"})
            else:
                conflicts.append({**public_row, "reason": "already_linked_to_different_toast"})

        if conflicts:
            db.rollback()
            return jsonify({
                "ok": False,
                "error": "existing link conflicts",
                "conflicts": conflicts,
                "written_count": 0,
            }), 409

        if not dry_run:
            now = datetime.utcnow()
            for row in LINK18:
                existing = (
                    db.query(CenaToastLink)
                    .filter_by(
                        cena_employee_id=row["cena_employee_id"],
                        store_key=row["store_key"],
                    )
                    .first()
                )
                # Synchronize columns directly on Employee model (Sam #3250)
                emp = db.query(Employee).filter_by(id=row["cena_employee_id"]).first()
                if emp:
                    emp.toast_employee_guid = row["toast_id"]
                    emp.toast_employee_name = row["toast_name"]

                if existing is not None:
                    continue
                db.add(CenaToastLink(
                    cena_employee_id=row["cena_employee_id"],
                    store_key=row["store_key"],
                    toast_id=row["toast_id"],
                    toast_name=row["toast_name"],
                    confirmed_by=None,
                    confirmed_at=now,
                ))
            db.commit()
        else:
            db.rollback()

        return jsonify({
            "ok": True,
            "dry_run": dry_run,
            "allowlist_size": len(LINK18),
            "written_count": 0 if dry_run else len(written),
            "would_write_count": len(written),
            "skipped_same_count": len(skipped_same),
            "written": written,
            "skipped_same": skipped_same,
            "rollback": "delete the newly inserted cena_toast_link rows for the returned cena_employee_id/store_key pairs",
        }), 200
    finally:
        db.close()
