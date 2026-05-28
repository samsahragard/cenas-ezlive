"""docck v1 seed function — idempotent first-boot row insertion.

Drop into app/services/docck_seed.py (new file). Call from app/__init__.py
after Base.metadata.create_all completes:

    try:
        from app.services.docck_seed import seed_docck_agents
        seed_docck_agents()
    except Exception:
        logging.getLogger(__name__).exception("docck seed failed (non-fatal)")
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.db import SessionLocal
from app.models import DocckAgent

log = logging.getLogger(__name__)


# Pre-computed werkzeug hashes of:
#   CENA_HEARTBEAT_TOKEN = 6915116858fcce2a55ef2ac0b76d9d782d65c20579f2ebf6c1d5d1e51b68c54e
#   PWCK_HEARTBEAT_TOKEN = fe1c663f9b7c8ceed192fbf7a1bb65fa18d8136952681ef15a665adb22d90c9d
# (samai minted 2026-05-27, handoff doc at C:\Users\sam\Desktop\docck_tokens_handoff_2026-05-27.md)
_CENA_HASH = "scrypt:32768:8:1$xw8IuqjHfqP6QmCu$d48a8f262b5648339e585f1239c5b82f3f18ca360c395a639aae9caf6d0e5bedc63013b5ffc997c6bb4a09144330c4a7d0bf88724a6d433a59866bf6ebd93f62"
_PWCK_HASH = "scrypt:32768:8:1$qduHU5kNIboeRhD2$5577b84c3c67850b6b5c144333cd8dd42c1ff72e009c14c757162ff13bb9ff40d1cd58474533e419c39bd1e897f3a36200b4dcf04a68c1369156a3ea930a0c22"


# Real Scheduled Task names on aick (per aick #1255 naming reconciliation —
# there is NO 'cena_service'/'cena_gateway' task; the actual CamelCase names are):
#   CenaGateway, CenaTaskPinger, CenaHubRelay, CenaSamChat, CenaChatWatcher, CenaHeartbeatSender
_CENA_SERVICES = {
    "primary": "CenaGateway",
    "watchers": ["CenaChatWatcher", "CenaHubRelay", "CenaSamChat", "CenaHeartbeatSender"],
}
_CENA_RESTART_SEQUENCE = [
    # Step 1: restart the core gateway (most-likely failure point) — cheap, fast.
    {"action": "restart_service",  "service_name": "CenaGateway",                                                                          "wait_seconds": 30},
    # Step 2: restart the gateway + all watchers together.
    {"action": "restart_services", "service_names": ["CenaGateway", "CenaChatWatcher", "CenaHubRelay", "CenaSamChat", "CenaHeartbeatSender"], "wait_seconds": 90},
    # Step 3: last resort — reboot the whole machine.
    {"action": "reboot_machine",                                                                                                            "wait_seconds": 360},
]

# pwck on Mini_IT13. ck is NSSM-wrapping the daemon as service name 'pwck_service'
# (per ck #1253). Until that wrap lands, restart_service no-ops + sequence advances
# to reboot — acceptable fallback.
_PWCK_SERVICES = {"primary": "pwck_service", "watchers": ["PwckHeartbeatSender"]}
_PWCK_RESTART_SEQUENCE = [
    {"action": "restart_service",  "service_name": "pwck_service",                          "wait_seconds": 30},
    {"action": "restart_services", "service_names": ["pwck_service", "PwckHeartbeatSender"], "wait_seconds": 90},
    {"action": "reboot_machine",                                                            "wait_seconds": 360},
]


_SPECS = (
    {
        "id": "cena",
        "display_name": "Cena",
        "machine_label": "AiCk",
        "watchdog_url": "http://100.108.119.19:8767",
        "watchdog_secret_env_var": "WATCHDOG_AICK_SECRET",
        "heartbeat_token_hash": _CENA_HASH,
        "services_json": _CENA_SERVICES,
        "restart_sequence_json": _CENA_RESTART_SEQUENCE,
        "enabled": True,
        "alert_dev_chat": True,
        "alert_telegram_threshold_seconds": 300,
    },
    {
        "id": "pwck",
        "display_name": "pwck",
        "machine_label": "Mini_IT13",
        "watchdog_url": "http://100.73.38.82:8767",
        "watchdog_secret_env_var": "WATCHDOG_CK_SECRET",
        "heartbeat_token_hash": _PWCK_HASH,
        "services_json": _PWCK_SERVICES,
        "restart_sequence_json": _PWCK_RESTART_SEQUENCE,
        "enabled": True,
        "alert_dev_chat": True,
        "alert_telegram_threshold_seconds": 300,
    },
)

# These fields are RECONCILED on every boot (force-set to canonical values) so a
# spec change (e.g. the #1255 task-name fix) propagates to existing rows without a
# manual DB edit. Identity/auth fields (heartbeat_token_hash) are NOT reconciled —
# only set on insert — so a token rotation is a deliberate, separate operation.
_RECONCILE_FIELDS = ("services_json", "restart_sequence_json", "watchdog_url", "watchdog_secret_env_var")


def seed_docck_agents() -> dict:
    """Idempotent. Inserts cena + pwck rows if missing; reconciles the
    restart/services/watchdog fields on existing rows to canonical values."""
    sess = SessionLocal()
    inserted: list[str] = []
    reconciled: list[str] = []
    try:
        for spec in _SPECS:
            existing = sess.get(DocckAgent, spec["id"])
            if existing is None:
                sess.add(DocckAgent(**spec))
                inserted.append(spec["id"])
                continue
            # Reconcile canonical fields (task-name fix per aick #1255, etc.)
            changed = False
            for field in _RECONCILE_FIELDS:
                if getattr(existing, field) != spec[field]:
                    setattr(existing, field, spec[field])
                    changed = True
            if changed:
                reconciled.append(spec["id"])
        sess.commit()
        log.info("docck seed: inserted=%s reconciled=%s", inserted, reconciled)
        return {"inserted": inserted, "reconciled": reconciled}
    except Exception:
        sess.rollback()
        log.exception("docck seed failed")
        raise
    finally:
        sess.close()
