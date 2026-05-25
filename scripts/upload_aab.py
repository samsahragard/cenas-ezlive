"""upload_aab.py — upload a signed Android App Bundle to Google Play
Console via the Android Publisher API.

Built 2026-05-25 (Sam #900 → ck #957/960 chain): companion to
mobile-android.yml. The workflow builds + signs the .aab; this script
uploads it to a track (default: internal testing) so Sam can update the
app on his phone without manually drag-and-dropping the bundle in Play
Console.

Service account: ck-play-publisher@favorable-valor-493419-r9.iam.gserviceaccount.com
Granted Play Console App permissions on com.cenaskitchen.app (13 perms
including 'Release apps to testing tracks').

Usage (local):
    python scripts/upload_aab.py --aab path/to/app.aab --track internal

Usage (CI, with creds from a secret):
    Set env GOOGLE_PLAY_PUBLISHER_JSON to the FULL JSON contents
    (preferred for GitHub Actions — drop the file into runner via
    `printf '%s' "$GOOGLE_PLAY_PUBLISHER_JSON" > /tmp/play.json`,
    then pass --creds /tmp/play.json).

Flags:
    --aab PATH        (required) path to the signed .aab
    --track NAME      internal | alpha | beta | production (default: internal)
    --creds PATH      service-account JSON path (default: looks at
                      $GOOGLE_PLAY_PUBLISHER_JSON_PATH, then
                      ~/.openclaw/.secrets/play_publisher.json)
    --release-notes T  one-line release notes (en-US) attached to the new release
    --package PKG     Android package name (default: com.cenaskitchen.app)
    --status STATUS   draft | completed | inProgress | halted (default: completed
                      — pushes the bundle live on the track immediately)
    --dry-run         create the edit + upload the bundle but DON'T commit
                      (useful for sanity-checking on a non-prod track)

Exit codes:
    0  success — release published on the track
    1  bad input / file missing
    2  auth failure (403 / invalid creds)
    3  upload failed (4xx / 5xx during bundle upload)
    4  commit failed
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("UPLOAD_AAB_LOGLEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("upload-aab")

DEFAULT_PACKAGE = "com.cenaskitchen.app"
DEFAULT_TRACK = "internal"
DEFAULT_CREDS_CANDIDATES = [
    os.environ.get("GOOGLE_PLAY_PUBLISHER_JSON_PATH"),
    r"C:\Users\sam\.openclaw\.secrets\play_publisher.json",
    str(Path.home() / ".openclaw" / ".secrets" / "play_publisher.json"),
]
SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]


def _resolve_creds(arg_path: str | None) -> Path:
    if arg_path:
        p = Path(arg_path)
        if not p.exists():
            log.error("credentials file not found: %s", p)
            sys.exit(1)
        return p
    for cand in DEFAULT_CREDS_CANDIDATES:
        if not cand:
            continue
        p = Path(cand)
        if p.exists():
            return p
    log.error("no credentials file found; pass --creds or set "
              "GOOGLE_PLAY_PUBLISHER_JSON_PATH, or place a play_publisher.json "
              "at ~/.openclaw/.secrets/")
    sys.exit(1)


def upload(aab_path: Path, *, track: str, creds_path: Path,
           package: str, release_notes: str | None,
           status: str, dry_run: bool) -> int:
    """Run the Edit -> Upload -> Track -> Commit flow. Returns exit code."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:
        log.error("missing google-api-python-client (pip install google-api-python-client google-auth): %s", e)
        return 1
    if not aab_path.exists():
        log.error(".aab not found: %s", aab_path)
        return 1
    log.info("auth via %s", creds_path)
    creds = service_account.Credentials.from_service_account_file(
        str(creds_path), scopes=SCOPES)
    service = build("androidpublisher", "v3", credentials=creds,
                    cache_discovery=False)
    log.info("package=%s track=%s aab=%s (%s bytes) status=%s dry_run=%s",
             package, track, aab_path.name,
             f"{aab_path.stat().st_size:,}", status, dry_run)

    # Step 1: open an edit
    try:
        edit = service.edits().insert(packageName=package, body={}).execute()
    except HttpError as e:
        log.error("edits.insert failed (auth?): %s", str(e)[:400])
        return 2
    edit_id = edit["id"]
    log.info("edit opened id=%s expires=%s", edit_id, edit.get("expiryTimeSeconds"))

    try:
        # Step 2: upload the bundle
        log.info("uploading bundle...")
        media = MediaFileUpload(str(aab_path),
                                mimetype="application/octet-stream",
                                resumable=True)
        try:
            bundle = service.edits().bundles().upload(
                packageName=package, editId=edit_id, media_body=media,
            ).execute()
        except HttpError as e:
            log.error("bundles.upload failed: %s", str(e)[:400])
            return 3
        version_code = bundle.get("versionCode")
        log.info("bundle uploaded versionCode=%s sha1=%s",
                 version_code, bundle.get("sha1"))

        # Step 3: assign to the track
        release: dict = {
            "name": f"v{version_code} ({time.strftime('%Y-%m-%d %H:%M')})",
            "versionCodes": [str(version_code)],
            "status": status,
        }
        if release_notes:
            release["releaseNotes"] = [
                {"language": "en-US", "text": release_notes[:500]},
            ]
        try:
            service.edits().tracks().update(
                packageName=package, editId=edit_id, track=track,
                body={"releases": [release]},
            ).execute()
        except HttpError as e:
            log.error("tracks.update failed: %s", str(e)[:400])
            return 3
        log.info("track %r updated with release %r", track, release["name"])

        # Step 4: commit (or skip if dry-run)
        if dry_run:
            log.info("DRY RUN — not committing. Discarding the edit.")
            try:
                service.edits().delete(packageName=package, editId=edit_id).execute()
            except HttpError as e:
                log.warning("dry-run cleanup delete failed (non-fatal): %s", e)
            return 0
        try:
            committed = service.edits().commit(
                packageName=package, editId=edit_id).execute()
        except HttpError as e:
            log.error("edits.commit failed: %s", str(e)[:400])
            return 4
        log.info("COMMITTED. edit_id=%s versionCode=%s track=%s",
                 committed.get("id"), version_code, track)
        return 0

    except Exception:
        # If anything explodes after the edit was opened, try to clean up.
        log.exception("flow crashed — attempting to delete the open edit")
        try:
            service.edits().delete(packageName=package, editId=edit_id).execute()
            log.info("orphan edit %s deleted", edit_id)
        except Exception:
            log.exception("delete also failed; orphan edit may linger")
        return 4


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aab", required=True, help="path to signed .aab")
    ap.add_argument("--track", default=DEFAULT_TRACK,
                    choices=["internal", "alpha", "beta", "production"])
    ap.add_argument("--creds", default=None,
                    help="path to service-account JSON")
    ap.add_argument("--release-notes", default=None)
    ap.add_argument("--package", default=DEFAULT_PACKAGE)
    ap.add_argument("--status", default="completed",
                    choices=["draft", "completed", "inProgress", "halted"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    creds_path = _resolve_creds(args.creds)
    return upload(
        Path(args.aab),
        track=args.track,
        creds_path=creds_path,
        package=args.package,
        release_notes=args.release_notes,
        status=args.status,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
