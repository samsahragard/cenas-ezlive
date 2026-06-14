#!/usr/bin/env python3
# ============================================================================
#  CENA CLOUD - DB Snapshot Receiver (Render service; Gate A of migration)
#  Single file. Stdlib only (sqlite3 ok). No installs, no dependencies.
#
#  Forked from the proven cenas_vault_cloud.py receiver. This service is ONLY
#  a data receiver: it RECEIVES CENA's database snapshots (.sqlite files, up
#  to ~200 MB each) pushed up from the local PC over the authenticated
#  /sync/* endpoints. There is NO file-browser UI. The CENA brain logic is
#  layered on top later; this is just the DB-receiver skeleton.
#
#  Same hardening as the vault receiver:
#    - Env-only config: PORT, CENA_CLOUD_ROOT, CENA_CLOUD_DB,
#      CENA_CLOUD_TOKEN (required). Binds 0.0.0.0.
#    - HTTP Basic auth (user "sam", password CENA_CLOUD_TOKEN) on EVERY
#      route except the unauthenticated /healthz health check.
#    - /healthz precedes the auth wall so Render's router keeps the instance
#      in rotation (hard-won lesson: an all-auth-walled service flaps).
#    - sha256-verified /sync/file uploads, atomic temp+os.replace writes,
#      quarantine-on-overwrite (never destroy), /sync/manifest, tombstones.
#    - ThreadingHTTPServer; the sqlite manifest/log lives on the persistent
#      disk (/var/data), never beside the script (repo dir is ephemeral).
#
#  RUN
#    CENA_CLOUD_TOKEN=... python cena_cloud.py   -> http://0.0.0.0:10000
# ============================================================================

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Make the vendored CENA L3 engine importable. cena_engine/ is a sibling package
# next to this script; ensure the script's own directory is on sys.path so
# `import cena_engine.cena_sql_orchestrator` resolves regardless of CWD. The
# engine itself is imported lazily inside the /assistant/answer handler so a
# missing/broken engine never blocks the data-receiver routes or /healthz.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# --- Env-only config (Render injects PORT; disk is mounted at /var/data). --
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT") or 10000)
CENA_CLOUD_ROOT = os.environ.get("CENA_CLOUD_ROOT") or "/var/data/cena-db"
CENA_CLOUD_DB = os.environ.get("CENA_CLOUD_DB") or "/var/data/cena_cloud_sync.db"
CENA_CLOUD_TOKEN = os.environ.get("CENA_CLOUD_TOKEN") or ""
if not CENA_CLOUD_TOKEN:
    print("ERROR: CENA_CLOUD_TOKEN environment variable is required "
          "(Basic auth password).")
    sys.exit(2)

AUTH_USER = b"sam"
AUTH_PASS = CENA_CLOUD_TOKEN.encode("utf-8")

os.makedirs(CENA_CLOUD_ROOT, exist_ok=True)

# These are .sqlite DB files up to ~200 MB; allow large bodies but keep a
# hard ceiling so a runaway upload cannot exhaust the disk in one request.
MAX_BODY = 600 * 1024 * 1024     # 600 MB cap on a single uploaded body
HASH_CHUNK = 1024 * 1024         # 1 MB streaming chunk

# ---------------------------------------------------------------------------
# Path safety: resolve a relpath strictly inside CENA_CLOUD_ROOT.
# ---------------------------------------------------------------------------
_ROOT_REAL = os.path.realpath(CENA_CLOUD_ROOT)
_ROOT_KEY = os.path.normcase(_ROOT_REAL)


def safe_path(p):
    """Resolve p and confirm it lives inside CENA_CLOUD_ROOT."""
    if not p:
        return None
    rp = os.path.realpath(p)
    key = os.path.normcase(rp)
    if key == _ROOT_KEY or key.startswith(_ROOT_KEY + os.sep):
        return rp
    return None


def sync_dest(relpath):
    """Normalize a forward-slash relpath; resolve it inside CENA_CLOUD_ROOT.
    Returns (clean_relpath, absolute_dest) or (None, None) if it escapes.
    Rejects path traversal (.., .) and drive letters (':')."""
    rel = str(relpath or "").replace("\\", "/").strip().strip("/")
    if not rel:
        return None, None
    parts = rel.split("/")
    if any(p in ("", ".", "..") or ":" in p for p in parts):
        return None, None
    dest = safe_path(os.path.join(CENA_CLOUD_ROOT, *parts))
    if not dest:
        return None, None
    return "/".join(parts), dest


def quarantine(dest, rel):
    """Move an existing file aside instead of destroying it."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    qpath = os.path.join(CENA_CLOUD_ROOT, "quarantine", stamp, *rel.split("/"))
    os.makedirs(os.path.dirname(qpath), exist_ok=True)
    final, n = qpath, 1
    while os.path.exists(final):
        final = qpath + ".%d" % n
        n += 1
    os.replace(dest, final)
    return final


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Sync store: sqlite3 manifest/log for the /sync/* endpoints. The local agent
# pushes DB-snapshot bytes up; this DB is the manifest of what the cloud holds.
# ---------------------------------------------------------------------------
def db_connect():
    return sqlite3.connect(CENA_CLOUD_DB, timeout=30)


def db_init():
    parent = os.path.dirname(CENA_CLOUD_DB)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = db_connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS files ("
            "relpath TEXT PRIMARY KEY, size INTEGER, sha256 TEXT, "
            "mtime REAL, deleted INTEGER DEFAULT 0)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS log ("
            "ts TEXT, action TEXT, relpath TEXT, result TEXT)")
        conn.commit()
    finally:
        conn.close()


def db_log(conn, action, relpath, result):
    conn.execute(
        "INSERT INTO log (ts, action, relpath, result) VALUES (?, ?, ?, ?)",
        (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         action, relpath, result))


def db_totals():
    """Count and total bytes of non-deleted files (for the / status page)."""
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files "
            "WHERE deleted = 0").fetchone()
    finally:
        conn.close()
    return int(row[0] or 0), int(row[1] or 0)


# ===========================================================================
#  HTTP handler
# ===========================================================================
class CenaCloudHandler(BaseHTTPRequestHandler):
    server_version = "CenaCloud/1.0"

    def log_message(self, *args):
        pass

    # -- auth -----------------------------------------------------------------
    # Every route except /healthz requires HTTP Basic auth (user "sam",
    # password CENA_CLOUD_TOKEN). Both halves are compared with
    # hmac.compare_digest. Failures get a bare 401 {"ok": false} and nothing
    # else - no route hints, no stack traces.
    def _authed(self):
        hdr = self.headers.get("Authorization", "")
        if hdr.startswith("Basic "):
            try:
                raw = base64.b64decode(hdr[6:].strip().encode("ascii"),
                                       validate=True)
            except Exception:
                raw = None
            if raw is not None:
                user, _, pw = raw.partition(b":")
                ok_user = hmac.compare_digest(user, AUTH_USER)
                ok_pass = hmac.compare_digest(pw, AUTH_PASS)
                if ok_user and ok_pass:
                    return True
        return self._send_401()

    def _send_401(self):
        body = b'{"ok": false}'
        try:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="cena-cloud"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass
        self.close_connection = True
        return False

    # -- token auth for the app relay ---------------------------------------
    # The live app relay authenticates to /assistant/answer with
    #   Authorization: Bearer <AI_ASSISTANT_CK_RUNTIME_TOKEN>
    #   X-Ai-Assistant-Token: <same>
    # The cutover sets that token == CENA_CLOUD_TOKEN. So /assistant/answer
    # accepts EITHER the existing Basic sam:CENA_CLOUD_TOKEN (via _authed) OR a
    # Bearer/X-Ai-Assistant-Token equal to CENA_CLOUD_TOKEN. Both halves are
    # compared with hmac.compare_digest. /sync/* and / stay Basic-only.
    def _bearer_or_basic_authed(self):
        if self._token_match():
            return True
        return self._authed()  # falls back to Basic; emits 401 on failure

    def _token_match(self):
        hdr = self.headers.get("Authorization", "")
        token = ""
        if hdr.lower().startswith("bearer "):
            token = hdr[7:].strip()
        if not token:
            token = (self.headers.get("X-Ai-Assistant-Token") or "").strip()
        if not token:
            return False
        return hmac.compare_digest(
            token.encode("utf-8"), CENA_CLOUD_TOKEN.encode("utf-8"))

    # -- helpers ------------------------------------------------------------
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj).encode("utf-8"))

    def _qs(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _drain(self):
        """Discard the request body so the connection stays usable. Used when
        rejecting an upload before reading its bytes for the hash."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        remaining = n
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, HASH_CHUNK))
            if not chunk:
                break
            remaining -= len(chunk)

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        # UNAUTHENTICATED health check (must precede the auth wall) so Render's
        # router has a definitive 200 signal and keeps this instance in
        # rotation. Leaks nothing -- static {"ok": true}. Set as the service
        # healthCheckPath. Without it, an all-auth-walled service gives the
        # edge no positive HTTP signal and routing flaps ("no-server" 404s).
        if urllib.parse.urlsplit(self.path).path == "/healthz":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
            return
        if not self._authed():
            return
        try:
            self._get_routes()
        except Exception:
            try:
                self._json({"ok": False, "error": "server error"}, 500)
            except Exception:
                pass

    def _get_routes(self):
        route = urllib.parse.urlsplit(self.path).path
        if route == "/":
            count, total = db_totals()
            self._json({"ok": True, "service": "cena-cloud",
                        "files": count, "bytes": total})
        elif route == "/sync/manifest":
            self._sync_manifest()
        elif route == "/sync/file":
            self._sync_file_get()
        else:
            self._json({"ok": False, "error": "not_found"}, 404)

    # -- POST ---------------------------------------------------------------
    # Auth is ROUTE-AWARE for POST. /assistant/answer accepts Basic OR a
    # Bearer/X-Ai-Assistant-Token equal to CENA_CLOUD_TOKEN (the app relay).
    # Every other POST route (/sync/*) stays Basic-only - nothing is weakened.
    def do_POST(self):
        route = urllib.parse.urlsplit(self.path).path
        if route == "/assistant/answer":
            if not self._bearer_or_basic_authed():
                return
        else:
            if not self._authed():
                return
        try:
            self._post_routes()
        except Exception:
            try:
                self._json({"ok": False, "error": "server error"}, 500)
            except Exception:
                pass

    def _post_routes(self):
        route = urllib.parse.urlsplit(self.path).path
        if route == "/sync/file":
            return self._sync_file_post()       # raw body, not JSON
        if route == "/sync/tombstone":
            return self._sync_tombstone()
        if route == "/sync/query_db":
            return self._sync_query_db()
        if route == "/assistant/answer":
            return self._assistant_answer()
        self._json({"ok": False, "error": "not_found"}, 404)

    # -- sync endpoints -------------------------------------------------------
    def _sync_manifest(self):
        conn = db_connect()
        try:
            rows = conn.execute(
                "SELECT relpath, size, sha256, mtime, deleted FROM files "
                "ORDER BY relpath").fetchall()
        finally:
            conn.close()
        files = [{"relpath": r[0], "size": r[1], "sha256": r[2],
                  "mtime": r[3], "deleted": r[4]} for r in rows]
        self._json({"ok": True, "files": files})

    def _sync_file_get(self):
        h = (self._qs().get("h") or [""])[0].strip().lower()
        if not h:
            return self._json({"ok": False, "error": "missing h"}, 400)
        conn = db_connect()
        try:
            row = conn.execute(
                "SELECT relpath FROM files WHERE sha256 = ? AND deleted = 0",
                (h,)).fetchone()
        finally:
            conn.close()
        if not row:
            return self._json({"ok": False, "error": "not_found"}, 404)
        rel, dest = sync_dest(row[0])
        if not dest or not os.path.isfile(dest):
            return self._json({"ok": False, "error": "not_found"}, 404)
        # Stream the file out in chunks (these are large DB snapshots).
        try:
            size = os.path.getsize(dest)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(dest, "rb") as f:
                for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
                    self.wfile.write(chunk)
        except OSError:
            # Headers may already be sent; nothing safe to add. Drop it.
            self.close_connection = True

    def _sync_file_post(self):
        sha = (self.headers.get("X-Sha256") or "").strip().lower()
        try:
            mtime = float(self.headers.get("X-Mtime") or "")
        except (TypeError, ValueError):
            self._drain()
            return self._json({"ok": False, "error": "bad X-Mtime"}, 400)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = -1
        if n < 0 or n > MAX_BODY:
            self._drain()
            return self._json({"ok": False, "error": "bad Content-Length"}, 400)

        # Resolve / validate the destination from headers BEFORE consuming the
        # body, so a path-traversal or drive-letter relpath is rejected cheap.
        rel, dest = sync_dest(self.headers.get("X-Relpath") or "")
        if not rel:
            self._drain()
            return self._json({"ok": False, "error": "relpath rejected"}, 400)
        if os.path.isdir(dest):
            self._drain()
            return self._json(
                {"ok": False, "error": "relpath is a directory"}, 400)
        if not sha:
            self._drain()
            return self._json({"ok": False, "error": "missing X-Sha256"}, 400)

        # Stream the body to a temp file in the SAME dir as dest while hashing
        # in chunks (bodies are up to ~200 MB - never buffer the whole thing).
        # If the sha mismatches we delete the temp and never touch the real
        # dest: nothing lands on disk on a bad upload.
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        h = hashlib.sha256()
        written = 0
        try:
            with open(tmp, "wb") as f:
                remaining = n
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, HASH_CHUNK))
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            self._cleanup(tmp)
            return self._json({"ok": False, "error": "write failed: %s" % e}, 500)

        if written != n or h.hexdigest() != sha:
            self._cleanup(tmp)
            return self._json({"ok": False, "error": "sha256 mismatch"}, 400)

        # Valid body confirmed in the temp file. On overwrite of a DIFFERENT
        # sha, move the existing file to quarantine first (never destroy).
        quarantined = None
        if os.path.isfile(dest):
            try:
                old_sha = file_sha256(dest)
            except OSError:
                old_sha = None
            if old_sha != sha:
                quarantined = quarantine(dest, rel)
        os.replace(tmp, dest)
        try:
            os.utime(dest, (mtime, mtime))
        except OSError:
            pass
        conn = db_connect()
        try:
            conn.execute(
                "INSERT INTO files (relpath, size, sha256, mtime, deleted) "
                "VALUES (?, ?, ?, ?, 0) "
                "ON CONFLICT(relpath) DO UPDATE SET size = excluded.size, "
                "sha256 = excluded.sha256, mtime = excluded.mtime, deleted = 0",
                (rel, written, sha, mtime))
            db_log(conn, "put", rel,
                   "ok" + (" quarantined-old" if quarantined else ""))
            conn.commit()
        finally:
            conn.close()
        self._json({"ok": True, "relpath": rel, "size": written,
                    "quarantined": bool(quarantined)})

    def _sync_tombstone(self):
        body = self._body()
        rel, dest = sync_dest(body.get("relpath", ""))
        if not rel:
            return self._json({"ok": False, "error": "relpath rejected"}, 400)
        quarantined = None
        if os.path.isfile(dest):
            quarantined = quarantine(dest, rel)
        conn = db_connect()
        try:
            cur = conn.execute(
                "UPDATE files SET deleted = 1 WHERE relpath = ?", (rel,))
            known = cur.rowcount > 0
            if not known:
                # Unknown relpath: record the tombstone anyway (idempotent ok).
                conn.execute(
                    "INSERT OR REPLACE INTO files "
                    "(relpath, size, sha256, mtime, deleted) "
                    "VALUES (?, 0, '', ?, 1)", (rel, time.time()))
            db_log(conn, "tombstone", rel,
                   ("ok" if known else "unknown-relpath")
                   + (" quarantined" if quarantined else ""))
            conn.commit()
        finally:
            conn.close()
        self._json({"ok": True, "relpath": rel, "known": known,
                    "quarantined": bool(quarantined)})

    def _sync_query_db(self):
        body = self._body()
        if not isinstance(body, dict):
            return self._json({"success": False, "error": "invalid body"}, 400)
        sql_query = body.get("sqlQuery")
        if not sql_query:
            return self._json({"success": False, "error": "missing 'sqlQuery'"}, 400)

        # Resolve DB paths inside CENA_CLOUD_ROOT
        toast_webhook_db = os.path.join(CENA_CLOUD_ROOT, "toast_webhook.sqlite")
        appdb_db = os.path.join(CENA_CLOUD_ROOT, "appdb.sqlite")
        toastdm_db = os.path.join(CENA_CLOUD_ROOT, "toastdm.sqlite")
        toast_db = os.path.join(CENA_CLOUD_ROOT, "toast.sqlite")

        if not os.path.exists(toast_webhook_db):
            return self._json({"success": False, "error": f"Sales database not found: {toast_webhook_db}"}, 404)

        # Open SQLite read-only connection
        db_uri = f"file:{os.path.abspath(toast_webhook_db).replace('\\', '/')}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # Attach snapshot databases read-only
            if os.path.exists(appdb_db):
                app_uri = f"file:{os.path.abspath(appdb_db).replace('\\', '/')}?mode=ro"
                conn.execute(f"ATTACH DATABASE '{app_uri}' AS appdb")
            if os.path.exists(toastdm_db):
                tdm_uri = f"file:{os.path.abspath(toastdm_db).replace('\\', '/')}?mode=ro"
                conn.execute(f"ATTACH DATABASE '{tdm_uri}' AS toastdm")
            if os.path.exists(toast_db):
                tst_uri = f"file:{os.path.abspath(toast_db).replace('\\', '/')}?mode=ro"
                conn.execute(f"ATTACH DATABASE '{tst_uri}' AS toast_labor")

            cursor = conn.cursor()
            cursor.execute(sql_query)
            rows = cursor.fetchall()
            results = [dict(r) for r in rows]
            return self._json({"success": True, "results": results})
        except Exception as e:
            return self._json({"success": False, "error": str(e)})
        finally:
            conn.close()

    # -- assistant endpoint --------------------------------------------------
    # POST /assistant/answer  body {"question": "...", "principal": {...?},
    # "context": {...?}} -> runs the vendored CENA L3 engine against the DB
    # snapshots already on disk and returns its bubble dict as JSON. Behind the
    # same Basic auth as every other route (the auth wall ran in do_POST). The
    # engine is imported here (lazy) and the whole call is wrapped so ANY engine
    # failure yields a clean 200 {"ok": false, "error": "..."} - never a 500
    # with a traceback, never a leaked stack.
    def _assistant_answer(self):
        # Full SUPERVISOR-shaped response, identical to the CK-local runtime.
        # The vendored cena_cloud_supervisor.answer(payload) wraps the legacy
        # answer path with 48h conversations, per-turn grading, DEV rescue, the
        # yes/no ladder, the DEV intro, the "Did I answer your question?"
        # follow-up, and the cena_active/ck_engaged observe-mode state machine.
        # It returns (body_dict, http_status); the dict is returned VERBATIM.
        #
        # Accept the app relay's full payload shape:
        #   {question, principal, tools, tool_data, route_path, route_meta,
        #    source, previous_question, previous_answer, routed_tool_id}
        body = self._body()
        if not isinstance(body, dict):
            return self._json({"ok": False, "error": "invalid body"}, 400)
        question = str(body.get("question") or "").strip()
        if not question:
            return self._json({"ok": False, "error": "missing 'question'"}, 400)
        if not isinstance(body.get("principal"), dict):
            # Default to a partner-level principal so an un-scoped probe still
            # reaches the analytics surface. The relay normally sends the real
            # authenticated principal.
            body = dict(body)
            body["principal"] = {
                "role": "partner",
                "kind": "partner",
                "is_owner_operator": True,
                "can_ask_operational": True,
                "source": "cena-cloud",
            }
        try:
            from cena_cloud_supervisor import answer
            result, status = answer(body)
            if not isinstance(result, dict):
                return self._json(
                    {"ok": False, "error": "supervisor returned no result"}, 200)
            return self._json(result, int(status) if status else 200)
        except Exception as e:  # never 500 with a traceback into the response
            return self._json(
                {"ok": False,
                 "error": "supervisor failure: %s: %s" % (type(e).__name__, e)},
                200)

    @staticmethod
    def _cleanup(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ===========================================================================
#  Entry point
# ===========================================================================
def main():
    # Env-only config; no CLI args, no UI, no push - this node only RECEIVES.
    db_init()
    server = ThreadingHTTPServer((HOST, PORT), CenaCloudHandler)
    print("cena-cloud listening on 0.0.0.0:%d root=%s" % (PORT, CENA_CLOUD_ROOT),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  cena-cloud closed.")


if __name__ == "__main__":
    main()
