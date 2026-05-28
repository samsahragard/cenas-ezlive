"""Driver re-assignment Selenium flow (Sam #669 + amendment).

Runs on AiCk (where Edge + the authed ezCater session live). NOT on
Render — Render only creates the driver_assignment_jobs row and serves
status to the polling frontend. A worker on AiCk
(scripts/driver_assigner_worker.py) picks up pending jobs and calls
run_assignment_flow() here.

The 9-step flow per Sam #669 with amendment (verify via DOM re-read,
not PDF parse):

  1. Launch isolated Edge context with the authed cookie jar
  2. Navigate to the order detail page
  3. Pop-up sweep — close any modal/banner overlay; if stuck → fresh
     context restart (Component 3a, max 2 retries)
  4. Click 'Unassign driver' button, wait for confirmation
  5. Click 'Change driver' to open the assign modal
  6. Search for new driver name, click radio, verify checked
  7. Click 'Assign driver', wait for modal close
  8. Navigate back to the order detail page (fresh GET, don't trust
     the modal close)
  9. Read the 'Assigned Driver' field from the DOM, compare to the
     picked driver name. Match → completed, no match → failed.

Selectors discovered against YXR-2AP on 2026-05-23: buttons render with
the literal text 'Unassign driver' / 'Change driver' / 'Assign driver'
(Tapas-Button class, no useful data-testid yet). Selectors are kept as
xpath text-matches so a className refactor on ezCater's side doesn't
break us.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

# Selenium is only needed on the AiCk gateway side where the Selenium
# flow actually runs. Render imports this module to call
# dispatch_assignment_job (HTTP POST only — no selenium). Wrapping
# the import in a try/except keeps Render's import path working even
# without selenium installed; the selenium-driving functions raise at
# runtime if you call them on a side that doesn't have it.
try:
    from selenium import webdriver
    from selenium.common.exceptions import (
        NoSuchElementException, TimeoutException, WebDriverException,
    )
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    _SELENIUM_OK = True
except ImportError:
    webdriver = None  # type: ignore
    NoSuchElementException = TimeoutException = WebDriverException = Exception  # type: ignore
    ChromeOptions = None  # type: ignore
    By = None  # type: ignore
    Keys = None  # type: ignore
    EC = None  # type: ignore
    WebDriverWait = None  # type: ignore
    _SELENIUM_OK = False

from app.db import get_db
from app.models import DriverAssignmentJob

logger = logging.getLogger(__name__)


# --- module config ---------------------------------------------------------

# Maps external_order_id (e.g. UK2-1EW) -> ezCater numeric order id.
# Built on the fly by walking the /orders list. Cached for the lifetime
# of the worker process so we don't re-walk for every job.
_ORDER_ID_CACHE: dict[str, str] = {}

# Where the authed cookies live on AiCk (per cenas_agent_setup_partner_password).
SESSION_FILE = Path(os.environ.get(
    "EZCATER_SESSION_FILE",
    str(Path(os.environ.get("USERPROFILE", "/root")) / ".openclaw/.secrets/ezcater_session.json"),
))

# Per-Selenium-driver-context user data dir. Sam #669 calls for an
# isolated context per attempt; we use a clean Chrome profile + load
# cookies from SESSION_FILE via driver.add_cookie() so the worker
# never shares state with the human Sam-on-his-desktop browser.
SELENIUM_PROFILE_DIR = Path(os.environ.get(
    "EZCATER_SELENIUM_PROFILE",
    r"C:\Users\sam\ezcater_assigner_chrome_profile",
))

PARTNER_PORTAL = "https://partnerportal.ezcater.com"

# This gateway's name (for gateway_processed column). Override via env
# on ck so failover hops are visible in the DB ('ck' / 'cena2').
GATEWAY_NAME = os.environ.get("EZCATER_GATEWAY_NAME", "aick")

# ck-side gateway URL for failover hops (Sam #669 Component 3b). Set
# this in the aick gateway's env (NOT in the Flask/Render env — only
# the AiCk worker calls into it). When unset, failover is skipped and
# the job lands as final-failed after 3 attempts — log line flags it.
CENA2_GATEWAY_URL = (os.environ.get("CENA2_GATEWAY_URL") or "").strip().rstrip("/")
CENA2_GATEWAY_TOKEN = os.environ.get("CENA2_GATEWAY_TOKEN", "")

# LAN team-chat hub for status pings (worker → dev chat). The hub is
# on Mini_IT13 (ck's PC) at 8765/lan; aick can also reach it via the
# tailnet. Override per env if the addr changes.
LANCHAT_HUB_URL = (
    os.environ.get("LANCHAT_HUB_URL") or "http://192.168.1.134:8765"
).rstrip("/")


# --- public entry points (called by worker + Render endpoint) -------------

def dispatch_assignment_job(job_id: str, order_id: str, current_driver: Optional[str],
                            new_driver: str) -> None:
    """HTTP-wake the aick gateway to run the flow (Sam #669 + 2026-05-24
    architecture choice b). Render-side endpoint calls this after
    writing the job row to its local DB. Aick gateway runs the flow
    + POSTs back to /catering/assign_driver/result with the outcome.

    Routes via CENA_PROXY (Render's userspace tailscaled SOCKS5
    proxy) when set, so 100.108.x.x Tailscale IPs are reachable from
    Render web service. Same pattern app/web/sam_chat.py uses for
    the cena_sam_chat mirror. Falls back to direct connection when
    CENA_PROXY is unset (e.g. local dev with the gateway on
    localhost).
    """
    # 2026-05-27 (Sam #1155 fix from samai's #1198): PWCK_PRIMARY_URL
    # is the new dispatch target for driver-assign. CENA_GATEWAY_URL is
    # reserved for /sam/chat -> Cena routing in sam_chat.py — the two
    # env vars collided earlier today when CENA_GATEWAY_URL was swung
    # to pwck:9000 (pwck has no /cena/stream, so Cena chat went dark).
    # New precedence: PWCK_PRIMARY_URL wins; CENA_GATEWAY_URL is the
    # legacy fallback to aick's Selenium gateway for emergency revert.
    gateway = (os.environ.get("PWCK_PRIMARY_URL")
               or os.environ.get("CENA_GATEWAY_URL")
               or "").strip().rstrip("/")
    gateway_src = "PWCK_PRIMARY_URL" if os.environ.get("PWCK_PRIMARY_URL") else "CENA_GATEWAY_URL"
    token = os.environ.get("CENA_GATEWAY_TOKEN", "").strip()
    callback = (os.environ.get("CENA_RENDER_ORIGIN") or "https://app.cenaskitchen.com").strip().rstrip("/")
    proxy = (os.environ.get("CENA_PROXY") or "").strip() or None
    if not gateway:
        logger.info("dispatch_assignment_job: PWCK_PRIMARY_URL + CENA_GATEWAY_URL both unset — job %s queued without wake", job_id)
        return
    body = {
        "job_id": job_id,
        "order_id": order_id,
        "current_driver": current_driver,
        "new_driver": new_driver,
        "callback_url": f"{callback}/catering/assign_driver/result",
    }
    headers = {"Content-Type": "application/json", "X-Cena-Token": token}
    try:
        import httpx
        client_kwargs: dict = {"timeout": httpx.Timeout(10.0, connect=5.0)}
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as hx:
            r = hx.post(f"{gateway}/jobs/driver-assign", json=body, headers=headers)
            logger.info(
                "dispatch_assignment_job: gateway responded %s for job %s (src=%s, url=%s, proxy=%s)",
                r.status_code, job_id, gateway_src, gateway, bool(proxy),
            )
            if r.status_code >= 300:
                logger.warning("dispatch body: %s", r.text[:200])
    except Exception:
        logger.exception("dispatch_assignment_job: gateway wake failed for job %s", job_id)


def run_assignment_flow_inline(order_id: str, current_driver: Optional[str],
                               new_driver: str,
                               job_id: Optional[str] = None) -> dict:
    """HTTP-callable entry point that takes the job payload directly
    (no DB row lookup needed) and returns a result dict. Used by the
    aick gateway's /jobs/driver-assign endpoint — the gateway HTTP-
    POSTs the result back to Render's callback endpoint so Render can
    update its own DB row.

    job_id (when provided) is used as the Chrome user-data-dir suffix
    so concurrent flows on the same gateway don't collide on Chrome's
    single-instance lock.

    Returns:
      {status: 'completed'|'failed', error_message: str|None,
       retry_count: int, gateway_processed: str}
    """
    last_error: Optional[str] = None
    attempts = 0
    final_status = "failed"
    # Truncate job_id to 12 chars for a cleaner dir name; full uuids are
    # noisy on disk + 12 is plenty unique within the active-job set.
    profile_suffix = (job_id[:12] if job_id else None)
    for attempts in range(3):
        try:
            _run_one_attempt_inline(order_id, new_driver,
                                    profile_suffix=profile_suffix)
            final_status = "completed"
            last_error = None
            break
        except _AuthExpired as e:
            last_error = f"auth_expired: {e}"; break
        except _PopupStuck as e:
            last_error = f"popup_stuck: {e}"; continue
        except _AssignmentMismatch as e:
            last_error = f"verification_mismatch: {e}"; break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"; continue
    return {
        "status": final_status,
        "error_message": last_error,
        "retry_count": attempts,
        "gateway_processed": GATEWAY_NAME,
    }


def _run_one_attempt_inline(order_id: str, new_driver: str,
                            profile_suffix: Optional[str] = None) -> None:
    """One Selenium pass, payload-driven (no DB).

    profile_suffix isolates the Chrome user-data-dir per concurrent job
    so back-to-back POSTs don't collide on Chrome's single-instance
    lock. Pass the job_id from the gateway."""
    driver = _make_driver(profile_suffix=profile_suffix)
    try:
        driver.get(PARTNER_PORTAL + "/")
        time.sleep(1)
        _inject_cookies(driver)
        numeric = _resolve_numeric_order_id(driver, order_id)
        order_url = f"{PARTNER_PORTAL}/orders/{numeric}"
        driver.get(order_url)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2.5)
        if "sign_in" in driver.current_url or "/login" in driver.current_url:
            raise _AuthExpired(f"redirected to {driver.current_url}")
        _dismiss_popups(driver)
        # ezCater Unassign button (frees the driver field server-side)
        try:
            _click_button_with_text(driver, "Unassign driver", timeout=8)
        except (TimeoutException, NoSuchElementException):
            pass  # field was already empty
        except WebDriverException:
            try:
                el = driver.find_element(By.XPATH, "//button[normalize-space()='Unassign driver']")
                driver.execute_script("arguments[0].click();", el)
            except Exception:
                pass
        time.sleep(1.0)
        for confirm_label in ("Yes, unassign", "Unassign", "Confirm", "Yes", "OK"):
            try:
                _click_button_with_text(driver, confirm_label, timeout=2)
                break
            except (TimeoutException, NoSuchElementException, WebDriverException):
                continue
        time.sleep(2.0)
        # Sam #833 2026-05-24: '__no_driver__' sentinel means unhook
        # only — skip the assign-driver modal, verify the field is
        # empty, done.
        if new_driver == "__no_driver__":
            driver.get(order_url)
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(2.5)
            body_text = driver.find_element(By.TAG_NAME, "body").text
            m = re.search(r"Assigned Driver\s*\n?\s*([^\n]+)", body_text)
            if m and m.group(1).strip():
                raise _AssignmentMismatch(
                    f"expected empty driver field after no-driver unhook, "
                    f"saw '{m.group(1).strip()}'"
                )
            return  # success
        # Open the assign-driver modal.
        opened = False
        for label in ("Assign in-house driver", "Assign driver", "Change driver"):
            try:
                _click_button_with_text(driver, label, timeout=5)
                opened = True; break
            except (TimeoutException, NoSuchElementException):
                continue
            except WebDriverException:
                try:
                    el = driver.find_element(By.XPATH, f"//button[normalize-space()='{label}']")
                    driver.execute_script("arguments[0].click();", el)
                    opened = True; break
                except Exception:
                    continue
        if not opened:
            raise RuntimeError("no assign-driver button found")
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'driver')]"))
        )
        time.sleep(1.0)
        # Type new driver name into search.
        search_box = None
        for sel in ["input[type='search']", "input[placeholder*='Search' i]",
                    "input[placeholder*='driver' i]"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                search_box = els[0]; break
        if search_box:
            search_box.clear()
            search_box.send_keys(new_driver)
            time.sleep(1.2)
        # Find + click matching label.
        match_el = None
        deadline = time.time() + 6.0
        while time.time() < deadline and match_el is None:
            for lab in driver.find_elements(By.CSS_SELECTOR, "label"):
                try:
                    txt = (lab.text or "").strip()
                    if not txt or len(txt) > 120:
                        continue
                    if _driver_names_match(txt, new_driver):
                        match_el = lab
                        logger.info("modal match: %r ~ %r", txt, new_driver)
                        break
                except Exception:
                    continue
            if match_el is None:
                time.sleep(0.4)
        if match_el is None:
            raise RuntimeError(f"driver '{new_driver}' not in modal list")
        try:
            match_el.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", match_el)
        # Click modal submit. Find inside the dialog to avoid the
        # page-level Unassign/Change driver buttons by accident.
        try:
            dlg = driver.find_element(By.CSS_SELECTOR, "[role='dialog']")
            for b in dlg.find_elements(By.CSS_SELECTOR, "button"):
                if (b.text or "").strip() == "Assign driver":
                    b.click(); break
        except Exception:
            _click_button_with_text(driver, "Assign driver", timeout=10)
        time.sleep(3)
        # Watch for the ezCater error dialog ("There was an error
        # assigning the driver.") — common on cross-kitchen assigns.
        try:
            for d in driver.find_elements(By.CSS_SELECTOR, "[role='dialog']"):
                if "error assigning the driver" in (d.text or "").lower():
                    raise _AssignmentMismatch(
                        "ezCater rejected the assignment: 'There was an "
                        "error assigning the driver.' (likely cross-"
                        "kitchen — driver's store doesn't match the "
                        "order's pickup store)"
                    )
        except _AssignmentMismatch:
            raise
        except Exception:
            pass
        # Reload + DOM re-read.
        driver.get(order_url)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2.5)
        body_text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r"Assigned Driver\s*\n?\s*([^\n]+)", body_text)
        if not m:
            raise _AssignmentMismatch(
                "no 'Assigned Driver' field found after assign — "
                "field still empty (assign didn't persist)"
            )
        actual = m.group(1).strip()
        if not _driver_names_match(actual, new_driver):
            raise _AssignmentMismatch(f"expected '{new_driver}', saw '{actual}'")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def run_assignment_flow(job_id: str) -> None:
    """Worker entry. Runs the 9-step Selenium flow for one job, updating
    the DB row to status=running while executing and to
    completed/failed at the end. Never raises — all failures land in
    DriverAssignmentJob.error_message."""
    db = next(get_db())
    try:
        job = (db.query(DriverAssignmentJob)
                 .filter(DriverAssignmentJob.job_id == job_id).first())
        if not job:
            logger.warning("run_assignment_flow: job %s not found", job_id)
            return
        if job.status != "pending":
            logger.info(
                "run_assignment_flow: job %s status=%s — not re-running",
                job_id, job.status,
            )
            return
        job.status = "running"
        job.started_at = datetime.utcnow()
        job.gateway_processed = GATEWAY_NAME
        db.commit()
        _flow_start = job.started_at
        _flow_order = job.order_id
        _flow_current = job.current_driver or "—"
        _flow_new = job.new_driver
    finally:
        db.close()
    _post_devchat(
        f"[catering] driver assignment job started — order {_flow_order}, "
        f"{_flow_current} -> {_flow_new} (job_id={job_id[:8]}…)"
    )

    # Up to (1 + 2) attempts: initial + 2 fresh-context retries per
    # Sam #669 Component 3a.
    last_error: Optional[str] = None
    attempts = 0
    final_status = "failed"
    for attempts in range(3):
        try:
            _run_one_attempt(job_id)
            final_status = "completed"
            last_error = None
            break
        except _AuthExpired as e:
            # Component 3c — auth_expired never retries.
            last_error = f"auth_expired: {e}"
            logger.exception("run_assignment_flow: auth expired on job %s", job_id)
            break
        except _PopupStuck as e:
            last_error = f"popup_stuck: {e}"
            logger.warning("run_assignment_flow: popup-stuck on job %s attempt %d",
                           job_id, attempts + 1)
            continue  # fresh context retry
        except _AssignmentMismatch as e:
            # Wrong driver showed up after re-read — don't retry; surface.
            last_error = f"verification_mismatch: {e}"
            break
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.exception("run_assignment_flow: error on job %s attempt %d",
                             job_id, attempts + 1)
            continue

    # ck failover hop (Sam #669 Component 3b) — only if final_status is
    # failed AND the failure isn't auth-expired (no point in retrying
    # auth from another machine, ck's session would also be dead) AND
    # we're not already running on the failover side.
    failover_attempted = False
    if (
        final_status == "failed"
        and not (last_error or "").startswith("auth_expired")
        and GATEWAY_NAME == "aick"
    ):
        if CENA2_GATEWAY_URL:
            failover_attempted = True
            ok = _failover_to_cena2(job_id)
            if ok:
                logger.info("ck failover accepted job %s — letting ck owns the row now", job_id)
                _post_devchat(
                    f"[catering] {_flow_order}: aick failed ({last_error or 'unknown'}); "
                    f"handed off to ck for retry."
                )
                return  # ck takes over from here, will set final status
            else:
                logger.warning("ck failover hop FAILED — leaving job final-failed")
                _post_devchat(
                    f"[catering] {_flow_order}: aick failed ({last_error or 'unknown'}); "
                    f"ck failover hop also failed — manual retry needed."
                )
        else:
            logger.warning(
                "CENA2_GATEWAY_URL not set — skipping failover (job %s stays failed). "
                "Set env on aick gateway to enable ck handoff.", job_id,
            )

    db = next(get_db())
    try:
        job = (db.query(DriverAssignmentJob)
                 .filter(DriverAssignmentJob.job_id == job_id).first())
        if not job:
            return
        job.status = final_status
        job.completed_at = datetime.utcnow()
        job.retry_count = attempts
        if final_status != "completed":
            job.error_message = last_error
        db.commit()
    finally:
        db.close()

    # Closing dev-chat log per Sam #669 spec.
    duration_s = int((datetime.utcnow() - _flow_start).total_seconds())
    if final_status == "completed":
        _post_devchat(
            f"[catering] driver assigned — {_flow_order}, {_flow_new} "
            f"verified via DOM re-read, took {duration_s}s"
        )
    else:
        urgent = ""
        if (last_error or "").startswith("auth_expired"):
            urgent = " URGENT: ezCater session expired — Sam needs to log in fresh."
        # Trim ugly multi-line stack tracebacks down to one line for chat;
        # the full traceback lives in the DB row + the worker log.
        clean = (last_error or "").splitlines()[0][:180] if last_error else "(no error message)"
        _post_devchat(
            f"[catering] driver assignment FAILED — {_flow_order}, "
            f"reason: {clean}, retries: {attempts}, "
            f"failover_attempted={failover_attempted}.{urgent}"
        )


# --- internal exceptions (raised by single-attempt; caller decides retry) -

class _AuthExpired(Exception):
    """Cookies dead — landed on a login page instead of the order page."""


class _PopupStuck(Exception):
    """Couldn't dismiss an overlay; retry from a fresh browser context."""


class _AssignmentMismatch(Exception):
    """Post-assign DOM re-read showed wrong driver. No retry."""


# --- one Selenium attempt -------------------------------------------------

def _run_one_attempt(job_id: str) -> None:
    """One pass through steps 1–9. Raises on any failure that should
    map onto a retry / final-failure category."""
    # Re-read job inside this attempt so a concurrent edit (e.g. an
    # admin marking it failed) is honored.
    db = next(get_db())
    try:
        job = (db.query(DriverAssignmentJob)
                 .filter(DriverAssignmentJob.job_id == job_id).first())
        if not job:
            raise RuntimeError(f"job {job_id} vanished mid-run")
        order_id = job.order_id
        new_driver = job.new_driver
    finally:
        db.close()

    driver = _make_driver()
    try:
        # Step 1.5 — establish ezcater.com navigation context, then
        # inject the authed cookie jar. add_cookie() requires a
        # matching domain to already be loaded.
        driver.get(PARTNER_PORTAL + "/")
        time.sleep(1)
        n_cookies = _inject_cookies(driver)
        logger.info("injected %d cookies into chrome", n_cookies)

        # Step 2 — navigate to the order detail page.
        numeric = _resolve_numeric_order_id(driver, order_id)
        order_url = f"{PARTNER_PORTAL}/orders/{numeric}"
        driver.get(order_url)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2.5)  # SPA hydration

        # Auth check — if we got bounced to /sign_in we're dead.
        if "sign_in" in driver.current_url or "/login" in driver.current_url:
            raise _AuthExpired(f"redirected to {driver.current_url}")

        # Step 3 — popup sweep.
        _dismiss_popups(driver)

        # Step 4 — app-side unhook FIRST per Sam 2026-05-23 rule
        # ("always UNHOOK the driver from the app side first, THEN go
        # assign driver on ezCater"). Clears our DB's
        # ezcater_driver_name so internal views aren't stale.
        _app_side_unhook(order_id)

        # Step 5 — ezCater-side: free the driver field. Sam 2026-05-23
        # "YOU MUST FREE UP EZCATER DRIVER FIELD" — clicking "Change
        # driver" alone updates-in-place without actually replacing the
        # assignment; the proper flow is Unassign + then Assign-in-house.
        try:
            _click_button_with_text(driver, "Unassign driver", timeout=8)
        except WebDriverException:
            # JS-click fallback for transient overlay intercept.
            el = driver.find_element(
                By.XPATH, "//button[normalize-space()='Unassign driver']")
            driver.execute_script("arguments[0].click();", el)
        time.sleep(1.0)

        # If a confirmation modal appears ("Are you sure?"), click its
        # primary confirm button. Common labels: 'Unassign', 'Confirm',
        # 'Yes, unassign', 'OK'. We try each; if none match within ~3s
        # we assume there was no confirm step.
        for confirm_label in ("Unassign", "Yes, unassign", "Confirm", "Yes", "OK"):
            try:
                _click_button_with_text(driver, confirm_label, timeout=2)
                logger.info("clicked unassign-confirm: '%s'", confirm_label)
                break
            except (TimeoutException, NoSuchElementException, WebDriverException):
                continue
        time.sleep(2.0)  # let the field clear

        # Step 6 — open the driver-picker modal. With the field freed,
        # the visible action is 'Assign in-house driver' (sometimes
        # also 'Assign driver').
        opened = False
        for label in ("Assign in-house driver", "Assign driver", "Change driver"):
            try:
                _click_button_with_text(driver, label, timeout=5)
                opened = True; break
            except (TimeoutException, NoSuchElementException):
                continue
            except WebDriverException:
                try:
                    el = driver.find_element(
                        By.XPATH, f"//button[normalize-space()='{label}']")
                    driver.execute_script("arguments[0].click();", el)
                    opened = True; break
                except Exception:
                    continue
        if not opened:
            raise RuntimeError("no assign-driver button found after unassign")

        # Wait for the modal driver list to render.
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'driver')]"))
        )
        time.sleep(1.0)

        # Step 6 — find + select the new driver. Try search input first.
        search_box = None
        for sel in [
            "input[type='search']",
            "input[placeholder*='Search' i]",
            "input[placeholder*='driver' i]",
        ]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                search_box = els[0]
                break
        if search_box:
            search_box.clear()
            search_box.send_keys(new_driver)
            time.sleep(1.2)

        # Per Sam 2026-05-23: type the name into the modal search box,
        # wait for the filter to narrow the list, then CLICK the
        # matching label (don't just leave it typed — the radio isn't
        # selected until the label is clicked). Modal labels carry a
        # 'CK #1 -' / 'CK#2-' / 'CK # 1' prefix that we need to ignore
        # when matching; _driver_names_match handles the variants.
        match_el = None
        deadline = time.time() + 6.0
        while time.time() < deadline and match_el is None:
            for lab in driver.find_elements(By.CSS_SELECTOR, "label"):
                try:
                    txt = (lab.text or "").strip()
                    if not txt or len(txt) > 120:
                        continue
                    if _driver_names_match(txt, new_driver):
                        match_el = lab
                        logger.info("modal match: '%s' ~ '%s'", txt, new_driver)
                        break
                except Exception:
                    continue
            if match_el is None:
                time.sleep(0.4)

        if match_el is None:
            raise RuntimeError(
                f"driver '{new_driver}' not in modal list (typed into search)"
            )
        try:
            match_el.click()
        except WebDriverException:
            driver.execute_script("arguments[0].click();", match_el)

        # Step 7 — confirm.
        _click_button_with_text(driver, "Assign driver", timeout=10)
        time.sleep(3)  # let the assignment settle

        # Step 8 — fresh GET on order page.
        driver.get(order_url)
        WebDriverWait(driver, 30).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
        time.sleep(2.5)

        # Step 9 — DOM re-read of Assigned Driver field.
        body_text = driver.find_element(By.TAG_NAME, "body").text
        m = re.search(r"Assigned Driver\s*\n?\s*([^\n]+)", body_text)
        if not m:
            raise _AssignmentMismatch(
                f"no 'Assigned Driver' field found after assign — body=\n{body_text[:600]}"
            )
        actual = m.group(1).strip()
        if not _driver_names_match(actual, new_driver):
            raise _AssignmentMismatch(
                f"expected '{new_driver}', saw '{actual}'"
            )
    finally:
        try:
            driver.quit()
        except Exception:
            pass


# --- helpers --------------------------------------------------------------

def _make_driver(profile_suffix: Optional[str] = None) -> webdriver.Chrome:
    """Launch a clean Chrome context + inject cookies from SESSION_FILE.
    Chrome is canonical on AiCk per [[cenas_canonical_browser_chrome]];
    the session cookies were captured from the Sam-driven Chrome login
    so they pair correctly here. Caller MUST call _inject_cookies()
    after navigating to ezcater.com — add_cookie requires a matching
    domain already loaded.

    Concurrency note (2026-05-26): when two jobs arrive within ~60s,
    they would share SELENIUM_PROFILE_DIR + the second hits
    SessionNotCreatedException because Chrome enforces single-instance
    per user-data-dir. profile_suffix carves a per-job subdirectory so
    concurrent attempts run in isolated profiles. Cookies are re-injected
    each call so the subdir doesn't need any persistent state."""
    if profile_suffix:
        profile_dir = SELENIUM_PROFILE_DIR / f"job_{profile_suffix}"
        profile_dir.mkdir(parents=True, exist_ok=True)
    else:
        profile_dir = SELENIUM_PROFILE_DIR
    opts = ChromeOptions()
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--window-size=1920,1200")
    # Headless is OK once selectors are stable; keep headed during build
    # so ezCater's bot-detection-style flagging doesn't surprise us.
    if os.environ.get("EZCATER_ASSIGNER_HEADLESS") == "1":
        opts.add_argument("--headless=new")
    return webdriver.Chrome(options=opts)


def _inject_cookies(driver: webdriver.Chrome) -> int:
    """Load cookies from ~/.openclaw/.secrets/ezcater_session.json into
    the current driver. Must be called AFTER driver.get(some ezcater.com
    URL) so add_cookie has a matching navigation context. Returns the
    count of cookies installed."""
    if not SESSION_FILE.exists():
        raise _AuthExpired(f"session file missing: {SESSION_FILE}")
    cookies = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    installed = 0
    for c in cookies:
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure")),
        }
        if c.get("httpOnly"):
            cookie["httpOnly"] = True
        exp = c.get("expires") or c.get("expiry")
        if isinstance(exp, (int, float)) and exp > 0:
            cookie["expiry"] = int(exp)
        try:
            driver.add_cookie(cookie)
            installed += 1
        except Exception:
            # Some cookies (e.g. third-party domain that doesn't match
            # the current navigation) will reject — skip them silently.
            continue
    return installed


def _resolve_numeric_order_id(driver: webdriver.Edge, external_id: str) -> str:
    """ezCater order URLs use numeric IDs (291986978) but Sam refers
    to orders by external_order_id (UK2-1EW). Walk /orders once and
    cache the mapping."""
    if external_id in _ORDER_ID_CACHE:
        return _ORDER_ID_CACHE[external_id]
    driver.get(f"{PARTNER_PORTAL}/orders")
    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/orders/"]'))
    )
    time.sleep(2)
    # The list page anchors carry the numeric href; the external_id is
    # rendered near the anchor. Build the mapping by walking each card.
    for a in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/orders/"]'):
        href = a.get_attribute("href") or ""
        m = re.search(r"/orders/(\d+)", href)
        if not m:
            continue
        numeric = m.group(1)
        try:
            # Walk up to a card container, grab its text — the external
            # order_id (XXX-XXX) lives in there.
            card = a
            for _ in range(5):
                card = card.find_element(By.XPATH, "./..")
                if any(s in (card.text or "") for s in ("Order #", "-")):
                    break
            card_text = (card.text or "")
        except Exception:
            card_text = ""
        for eid in re.findall(r"\b[0-9A-Z]{3}-[0-9A-Z]{3}\b", card_text):
            _ORDER_ID_CACHE[eid] = numeric
    if external_id not in _ORDER_ID_CACHE:
        # Fallback: open each order and read the page title until we
        # find the matching external_id. Slow but correct.
        for a in driver.find_elements(By.CSS_SELECTOR, 'a[href*="/orders/"]')[:25]:
            href = a.get_attribute("href") or ""
            m = re.search(r"/orders/(\d+)", href)
            if not m:
                continue
            numeric = m.group(1)
            driver.get(f"{PARTNER_PORTAL}/orders/{numeric}")
            time.sleep(1.5)
            m2 = re.search(r"Order # ([0-9A-Z]{3}-[0-9A-Z]{3})", driver.title or "")
            if m2:
                _ORDER_ID_CACHE[m2.group(1)] = numeric
                if m2.group(1) == external_id:
                    return numeric
    if external_id not in _ORDER_ID_CACHE:
        raise RuntimeError(f"could not resolve external_order_id {external_id} to numeric")
    return _ORDER_ID_CACHE[external_id]


def _dismiss_popups(driver: webdriver.Edge, timeout: float = 3.0) -> None:
    """Best-effort sweep of any modal overlay that might intercept
    clicks. Looks for common close-button shapes (aria-label='Close',
    text 'Got it' / 'Close' / 'X' / 'Dismiss' / 'OK'). If a popup is
    detected but can't be dismissed within `timeout` seconds, raises
    _PopupStuck so the caller can retry with a fresh context."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = driver.find_elements(
            By.CSS_SELECTOR,
            "button[aria-label='Close'],"
            " button[aria-label*='close' i],"
            " [role='dialog'] button"
        )
        if not candidates:
            return
        progress = False
        for c in candidates:
            try:
                if c.is_displayed():
                    txt = (c.text or "").strip().lower()
                    if txt in ("got it", "close", "dismiss", "ok", "no thanks", ""):
                        c.click()
                        progress = True
                        time.sleep(0.4)
                        break
            except Exception:
                continue
        if not progress:
            time.sleep(0.4)
    # If we got here a popup was visible the whole timeout window.
    overlays = driver.find_elements(By.CSS_SELECTOR, "[role='dialog']")
    if overlays:
        raise _PopupStuck(f"{len(overlays)} dialog(s) visible after {timeout}s sweep")


def _click_button_with_text(driver: webdriver.Edge, text: str, timeout: float = 10) -> None:
    """Click a button by its rendered text. xpath normalize-space() so
    leading/trailing whitespace + multiple internal spaces don't
    matter."""
    xp = f"//button[normalize-space()='{text}']"
    el = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((By.XPATH, xp))
    )
    el.click()


def _app_side_unhook(order_id: str) -> None:
    """Sam 2026-05-23: keep OUR DB consistent before touching ezCater.
    Clears ezcater_driver_name on the local Order row + makes a
    best-effort ezCater API unhook attempt (Path 1). Whether the API
    call succeeds or not, this function does NOT raise — the canonical
    ezCater-side unhook is the UI 'Unassign driver' click in
    _run_one_attempt (Path 2), which works for any courier id including
    Cenas Fajitas / store_3 variants that aren't in our static
    _COURIER_ID_FOR_STORE map (FYV-5XU 2026-05-23 case).

    Why both paths? The API call is faster + cleaner when it works.
    The UI click is universal. We try API first as a no-cost short-
    circuit, then the UI step in _run_one_attempt picks up the slack
    when API doesn't have the right courier id.
    """
    from app.models import Order

    db = next(get_db())
    try:
        order = (db.query(Order)
                   .filter(Order.external_order_id == order_id).first())
    finally:
        db.close()

    # Local DB clear (always, best effort).
    if order:
        db2 = next(get_db())
        try:
            row = (db2.query(Order)
                     .filter(Order.external_order_id == order_id).first())
            if row:
                row.ezcater_driver_name = None
                db2.commit()
                logger.info("app_side_unhook: cleared local ezcater_driver_name for %s", order_id)
        except Exception:
            logger.exception("app_side_unhook: local DB clear failed (non-fatal)")
        finally:
            db2.close()

    # Path 1: best-effort ezCater API unhook. Wrapped — any failure
    # falls through to the UI click in _run_one_attempt without
    # raising. Logs the outcome so a manual triage can spot a stuck
    # state if both paths fail.
    try:
        from app.web.orders_browse import (
            _try_unassign, _fetch_delivery_id_for_order,
            _COURIER_ID_FOR_STORE, _LEGACY_COURIER_ID_FOR_STORE,
        )
        delivery_id = order.external_delivery_id if order else None
        origin_store_id = order.origin_store_id if order else None
        if not delivery_id:
            delivery_id = _fetch_delivery_id_for_order(order_id)
        if not delivery_id:
            logger.info(
                "app_side_unhook: no delivery_id resolvable for %s — "
                "skipping API path, will rely on UI Unassign click", order_id,
            )
            return
        primary = _COURIER_ID_FOR_STORE.get(origin_store_id) if origin_store_id else None
        legacy = _LEGACY_COURIER_ID_FOR_STORE.get(origin_store_id) if origin_store_id else None
        if primary:
            candidates = [primary] + ([legacy] if legacy and legacy != primary else [])
        else:
            candidates = ["masood-ck-1", "sam-ck-2", "sam-ck-1", "masood-ck-2"]
        for cid in candidates:
            ok, err = _try_unassign(delivery_id, cid)
            if ok:
                logger.info("app_side_unhook: API unassigned %s on delivery %s",
                            cid, delivery_id)
                return
        logger.info(
            "app_side_unhook: API path tried %d courier ids for %s, none matched — "
            "UI Unassign click will handle it", len(candidates), order_id,
        )
    except Exception:
        logger.exception("app_side_unhook: API path raised (non-fatal)")


def _driver_names_match(actual: str, expected: str) -> bool:
    """Driver labels render with prefix/suffix variations:
       - 'Sam CK #2'  (name + kitchen)
       - 'Sam #2'     (name + disambiguation)
       - 'CK#2 Sam'   (kitchen prefix + name)
       - 'Tatiana'    (bare name)
    Strip the 'CK#N' / 'CK #N' kitchen markers from both sides, then
    require the remaining tokens to overlap — actual contains all of
    expected's words, OR vice versa. Matching is casefold."""
    def norm(s: str) -> set[str]:
        s = re.sub(r"CK\s*#\s*\d+", " ", s, flags=re.IGNORECASE)
        s = re.sub(r"[^\w#]", " ", s)
        return {tok.casefold() for tok in s.split() if tok}
    a, e = norm(actual), norm(expected)
    if not a or not e:
        return False
    return a.issubset(e) or e.issubset(a)


# --- dev-chat + failover plumbing (Sam #669 Component 3b/3c) --------------

def _post_devchat(body: str) -> None:
    """Best-effort post to the LAN team-chat hub (the channel Sam +
    the team watch). Never raises — chat being down doesn't fail an
    assignment job. Author 'aick' or 'ck' depending on which gateway
    is running."""
    try:
        data = urllib.parse.urlencode({
            "author": f"{GATEWAY_NAME}-assigner", "body": body,
        }).encode()
        req = urllib.request.Request(
            f"{LANCHAT_HUB_URL}/partner/developer/chat/post",
            data=data,
            headers={"User-Agent": "ezcater-assigner/1.0"},
        )
        with urllib.request.urlopen(req, timeout=6) as r:
            r.read(64)
    except Exception:
        logger.exception("post_devchat failed (non-fatal): %s", body[:80])


def _failover_to_cena2(job_id: str) -> bool:
    """Hand the job off to ck's gateway. Returns True iff ck accepted
    the job (HTTP 2xx). On accept, ck's worker will pick up the same
    DriverAssignmentJob row (gateway_processed is overwritten to 'ck'
    when ck's run_assignment_flow flips status='running')."""
    if not CENA2_GATEWAY_URL:
        return False
    # Reset the row to pending so ck's worker can claim it.
    db = next(get_db())
    try:
        job = (db.query(DriverAssignmentJob)
                 .filter(DriverAssignmentJob.job_id == job_id).first())
        if not job:
            return False
        job.status = "pending"
        job.started_at = None
        job.completed_at = None
        job.gateway_processed = None
        # retry_count NOT reset — preserves attempt history across hops
        db.commit()
    finally:
        db.close()

    try:
        payload = json.dumps({"job_id": job_id}).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ezcater-assigner-failover/1.0",
        }
        if CENA2_GATEWAY_TOKEN:
            headers["X-Cena-Token"] = CENA2_GATEWAY_TOKEN
        req = urllib.request.Request(
            f"{CENA2_GATEWAY_URL}/cena2/test/playwright",
            data=payload, headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception:
        logger.exception("failover_to_cena2 hop failed for job %s", job_id)
        return False
