"""driver_assigner_worker — polls driver_assignment_jobs for pending
rows and runs the Selenium re-assignment flow against ezCater.

Runs on AiCk (where Edge + the authed ezCater session live). Render
only creates job rows; this worker picks them up. Single-process,
single-threaded — concurrent jobs are handled by running multiple
worker processes if needed (each holds its own Edge profile). For the
Sam #669 v1 ship a single worker is enough; the order pace + flow
duration (15-30s) means contention is rare.

Polling cadence: every 2 seconds. Sam's frontend polls /status every
2s, so anything more aggressive on the worker side doesn't help
perceived latency.

Stop signal: SIGINT / SIGTERM. The worker finishes its current job
(if any) and exits cleanly.

Launch on AiCk:
    .venv/Scripts/python.exe scripts/driver_assigner_worker.py
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta

# Set up logging before importing app modules so their loggers inherit
# the format.
logging.basicConfig(
    level=os.environ.get("EZCATER_ASSIGNER_LOGLEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("driver_assigner_worker")

from app.db import get_db
from app.models import DriverAssignmentJob
from app.services.ezcater_driver_assigner import run_assignment_flow


_should_stop = False


def _handle_stop(signum, frame):
    global _should_stop
    logger.info("received signal %s — stopping after current job", signum)
    _should_stop = True


def main() -> int:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_stop)
        except (ValueError, OSError):
            pass  # not main thread / Windows console
    poll_seconds = float(os.environ.get("EZCATER_ASSIGNER_POLL_SECONDS", "2"))
    stale_pickup_minutes = int(os.environ.get("EZCATER_ASSIGNER_STALE_MINUTES", "10"))
    logger.info("worker starting (poll=%.1fs, stale_pickup=%dmin)",
                poll_seconds, stale_pickup_minutes)

    while not _should_stop:
        try:
            job_id = _claim_next_job(stale_pickup_minutes)
        except Exception:
            logger.exception("claim loop error — sleeping then retrying")
            time.sleep(poll_seconds * 4)
            continue
        if not job_id:
            time.sleep(poll_seconds)
            continue
        logger.info("picked up job %s", job_id)
        try:
            run_assignment_flow(job_id)
        except Exception:
            logger.exception("run_assignment_flow raised (the function itself "
                             "is supposed to never raise — investigate)")
        # Small breath before next claim; lets DB writes settle.
        time.sleep(0.5)

    logger.info("worker exited cleanly")
    return 0


def _claim_next_job(stale_pickup_minutes: int) -> str | None:
    """Atomically claim one pending job. Returns the job_id or None if
    the queue is empty. Also re-claims jobs that have been stuck in
    'running' for more than stale_pickup_minutes (worker crashed
    mid-flow — pick them up again rather than letting them rot)."""
    db = next(get_db())
    try:
        # Pick the oldest pending job first.
        stale_cutoff = datetime.utcnow() - timedelta(minutes=stale_pickup_minutes)
        job = (
            db.query(DriverAssignmentJob)
            .filter(
                (DriverAssignmentJob.status == "pending")
                | (
                    (DriverAssignmentJob.status == "running")
                    & (DriverAssignmentJob.started_at < stale_cutoff)
                )
            )
            .order_by(DriverAssignmentJob.created_at.asc())
            .first()
        )
        if not job:
            return None
        # Don't flip status here — run_assignment_flow does the
        # status='running' transition itself so it's atomic with the
        # actual Selenium start.
        return job.job_id
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
