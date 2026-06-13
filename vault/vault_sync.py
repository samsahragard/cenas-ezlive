r"""vault_sync.py - one-way-primary sync of the local Cenas vault to a cloud
vault node over HTTPS, with limited pull-down.

Designed to run as a 5-minute Task Scheduler job. Each invocation performs
one idempotent pass; the local vault app does NOT need to be running.

Direction of trust:
  - PUSH is primary: newest mtime wins, ties go to local; never overwrite a
    newer file with an older one in either direction.
  - PULL is limited: only genuinely cloud-new files (absent locally AND
    absent from local DB history) are downloaded, sha256-verified BEFORE
    writing, and land ONLY under <root>\99 Inbox\from-cloud\ - never
    anywhere else.
  - Tombstones never destroy local data: cloud-deleted files are MOVED to
    <root>\99 Inbox\from-cloud\_tombstoned\<relpath>, and only when the
    local sha matches the tombstoned sha; otherwise left in place and a
    conflict is logged.

Config (all beside this script):
  .env                 KEY=VALUE lines, # comments allowed. RENDER_VAULT_URL
                       and VAULT_TOKEN are required; process environment
                       overrides the file. Missing -> exit 2.
  vault_sync.allow     one allowed sync root per line. Created with exactly
                       C:\Cenas if absent. ONLY paths under an allow root
                       are ever touched.
  vault_sync.skip      one relative prefix per line to exclude (relative to
                       the allow root, case-insensitive, slash tolerant).
                       Created with "08 Archive" and "99 Inbox\from-cloud"
                       if absent - the latter prevents pull->push echo
                       loops.
  vault_sync_local.db  sqlite state: files(relpath PRIMARY KEY, size,
                       sha256, mtime, deleted INTEGER DEFAULT 0) and
                       log(ts, action, relpath, result). Every action gets
                       a log row (push, pull, tombstone, skip-blocklist,
                       skip-junction, conflict, error).

Cloud API contract (defined here; the server does not exist yet):
  GET  {BASE}/sync/manifest
       -> 200 JSON {"files": {"<relpath>": {"size": int,
                    "sha256": "<hex>", "mtime": <float>,
                    "deleted": 0 or 1}, ...}}
  POST {BASE}/sync/file        headers X-Relpath, X-Sha256, X-Mtime;
                               body = raw file bytes -> 2xx ({"ok": true})
  GET  {BASE}/sync/file?h=<sha256>   -> 200 raw file bytes
  POST {BASE}/sync/tombstone   JSON {"relpath": "<relpath>"} -> 2xx
  Every request carries  Authorization: Basic base64("sam:" + VAULT_TOKEN).
  401 on the manifest fetch -> clear error, exit 3.

Exit codes:
  0 pass completed (errors, if any, are counted in the summary line)
  2 missing config / bad usage
  3 auth failure (HTTP 401)
  4 cloud manifest unreachable or unparseable

Usage:
  python vault_sync.py [--once]     normal single pass (default)
  python vault_sync.py --dry-run    print would-be actions; NO network and
                                    NO writes (not even the sqlite db)
"""

import base64
import fnmatch
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env"
ALLOW_FILE = SCRIPT_DIR / "vault_sync.allow"
SKIP_FILE = SCRIPT_DIR / "vault_sync.skip"
DB_FILE = SCRIPT_DIR / "vault_sync_local.db"

DEFAULT_ALLOW_TEXT = "C:\\Cenas\n"
DEFAULT_SKIP_TEXT = "08 Archive\n99 Inbox\\from-cloud\n"

# Hard blocklist: matched (fnmatch, case-insensitive) against EVERY path
# segment and filename. Never pushed, never pulled, never tombstoned.
BLOCKLIST = [
    "TOOLS.md",
    "MEMORY.md",
    "memory",
    "state",
    "*conversations*",
    "*.env",
    "*credentials*",
    "*secrets*",
    ".git",
    "cenas_vault_data.json",
]

INBOX_REL = "99 Inbox/from-cloud"            # pull landing zone (rel, fwd /)
TOMB_REL = INBOX_REL + "/_tombstoned"        # cloud-tombstone parking lot

CHUNK = 1024 * 1024                          # sha256 streaming chunk size
HTTP_TIMEOUT = 60                            # seconds per request
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def aprint(msg, err=False):
    """ASCII-safe print (relpaths may contain non-ASCII characters)."""
    text = str(msg).encode("ascii", "backslashreplace").decode("ascii")
    print(text, file=(sys.stderr if err else sys.stdout), flush=True)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def parse_env_file(path):
    cfg = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            cfg[key.strip()] = val.strip().strip('"').strip("'")
    return cfg


def load_config():
    """RENDER_VAULT_URL / VAULT_TOKEN from process env (wins) or .env file."""
    file_cfg = parse_env_file(ENV_FILE)
    url = os.environ.get("RENDER_VAULT_URL") or file_cfg.get("RENDER_VAULT_URL")
    token = os.environ.get("VAULT_TOKEN") or file_cfg.get("VAULT_TOKEN")
    if not url or not token:
        aprint("ERROR: RENDER_VAULT_URL and VAULT_TOKEN must be set, either "
               "in the process environment or in %s" % ENV_FILE, err=True)
        sys.exit(2)
    return url.rstrip("/"), token


def load_list_file(path, default_text, dry_run):
    """Read one-entry-per-line file; create with defaults if absent."""
    if not path.exists():
        if dry_run:
            aprint("DRY-RUN note: %s missing, using built-in defaults "
                   "(file not created in dry-run)" % path.name)
            text = default_text
        else:
            path.write_text(default_text, encoding="ascii")
            text = default_text
    else:
        text = path.read_text(encoding="utf-8-sig")
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


# ---------------------------------------------------------------------------
# path helpers
# ---------------------------------------------------------------------------

def norm_rel(rel):
    """Lowercased, forward-slash, no leading/trailing slash."""
    return rel.replace("\\", "/").strip("/").lower()


def blocked_pattern(name):
    """Return the blocklist pattern a single segment/filename hits, or None."""
    low = name.lower()
    for pat in BLOCKLIST:
        if fnmatch.fnmatchcase(low, pat.lower()):
            return pat
    return None


def blocked_pattern_path(relpath):
    """Blocklist check across every segment of a relative path."""
    for seg in relpath.replace("\\", "/").split("/"):
        pat = blocked_pattern(seg)
        if pat:
            return pat
    return None


def skip_prefix_hit(rel_normed, skip_prefixes_normed):
    for pref in skip_prefixes_normed:
        if rel_normed == pref or rel_normed.startswith(pref + "/"):
            return pref
    return None


def safe_relpath(rel):
    """Sanitize a cloud-supplied relpath. None if it tries to escape."""
    rel = rel.replace("\\", "/").strip("/")
    if not rel or ":" in rel:
        return None
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def unique_dest(p):
    """Never clobber an existing file when parking tombstoned copies."""
    if not p.exists():
        return p
    i = 1
    while True:
        cand = p.with_name(p.name + ".%d" % i)
        if not cand.exists():
            return cand
        i += 1


def atomic_write(target, data, mtime):
    """Write bytes to target via temp-file + os.replace, then set mtime."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".vsync_tmp_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.utime(str(target), (mtime, mtime))


# ---------------------------------------------------------------------------
# run log (sqlite log table in live mode, stdout in dry-run)
# ---------------------------------------------------------------------------

class RunLog:
    def __init__(self, con, dry_run):
        self.con = con
        self.dry = dry_run
        self.counts = {}

    def log(self, action, relpath, result):
        self.counts[action] = self.counts.get(action, 0) + 1
        if self.dry:
            aprint("DRY-RUN %-14s %s  (%s)" % (action, relpath, result))
        else:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.con.execute(
                "INSERT INTO log(ts, action, relpath, result) VALUES (?,?,?,?)",
                (ts, action, relpath, result))
            if action == "error":
                aprint("ERROR %s: %s" % (relpath, result), err=True)

    def n(self, action):
        return self.counts.get(action, 0)


# ---------------------------------------------------------------------------
# local state db
# ---------------------------------------------------------------------------

def open_db(dry_run):
    if dry_run:
        if not DB_FILE.exists():
            return None  # no history yet; dry-run must not create the db
        uri = "file:%s?mode=ro" % str(DB_FILE).replace("\\", "/")
        return sqlite3.connect(uri, uri=True)
    con = sqlite3.connect(str(DB_FILE))
    con.execute("CREATE TABLE IF NOT EXISTS files("
                "relpath TEXT PRIMARY KEY, size INTEGER, sha256 TEXT, "
                "mtime REAL, deleted INTEGER DEFAULT 0)")
    con.execute("CREATE TABLE IF NOT EXISTS log("
                "ts TEXT, action TEXT, relpath TEXT, result TEXT)")
    con.commit()
    return con


def load_history(con):
    """{relpath: {"size":, "sha256":, "mtime":, "deleted":}} from files table."""
    history = {}
    if con is None:
        return history
    try:
        rows = con.execute(
            "SELECT relpath, size, sha256, mtime, deleted FROM files")
    except sqlite3.Error:
        return history
    for rel, size, sha, mtime, deleted in rows:
        history[rel] = {"size": size, "sha256": sha,
                        "mtime": mtime, "deleted": int(deleted or 0)}
    return history


# ---------------------------------------------------------------------------
# walk
# ---------------------------------------------------------------------------

def walk_root(root, skip_prefixes_normed, runlog, manifest):
    """os.scandir recursion. Junction-safe, blocklist- and skip-aware.

    Fills manifest[relpath (forward slashes)] = (size, sha256, mtime, root).
    """
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            entries = os.scandir(current)
        except OSError as exc:
            runlog.log("error", current, "scandir failed: %s" % exc)
            continue
        with entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    runlog.log("error", entry.path, "stat failed: %s" % exc)
                    continue
                # Junction / reparse point: NEVER descend or read.
                # (plain is_symlink() misses Windows junctions)
                attrs = getattr(st, "st_file_attributes", 0)
                if attrs & REPARSE:
                    runlog.log("skip-junction", entry.path, "reparse point")
                    continue
                # Hard blocklist on the entry name (parent segments were
                # already checked when we decided to descend into them).
                pat = blocked_pattern(entry.name)
                if pat:
                    runlog.log("skip-blocklist", entry.path,
                               "blocklist pattern: %s" % pat)
                    continue
                rel = os.path.relpath(entry.path, str(root))
                pref = skip_prefix_hit(norm_rel(rel), skip_prefixes_normed)
                if pref:
                    runlog.log("skip-blocklist", entry.path,
                               "vault_sync.skip prefix: %s" % pref)
                    continue
                if stat.S_ISDIR(st.st_mode):
                    stack.append(entry.path)
                elif stat.S_ISREG(st.st_mode):
                    try:
                        sha = sha256_of(entry.path)
                    except OSError as exc:
                        runlog.log("error", entry.path,
                                   "read failed: %s" % exc)
                        continue
                    manifest[rel.replace("\\", "/")] = (
                        st.st_size, sha, st.st_mtime, root)
                # other types (fifo etc.) are ignored silently


# ---------------------------------------------------------------------------
# http
# ---------------------------------------------------------------------------

# Render's edge intermittently returns 404 "no-server" / 502 / 503 while it
# rebalances routing to a healthy single instance. These are TRANSPORT blips,
# not real answers, so the worker retries them with backoff. Each urllib call
# is a fresh connection, so a retry usually lands on a good edge path.
_TRANSIENT = (404, 502, 503, 504, 0)
_MAX_TRIES = 5


def _http_once(base_url, token, method, path_qs, body=None, headers=None):
    req = urllib.request.Request(base_url + path_qs, data=body, method=method)
    cred = base64.b64encode(("sam:" + token).encode("utf-8")).decode("ascii")
    req.add_header("Authorization", "Basic " + cred)
    for key, val in (headers or {}).items():
        req.add_header(key, val)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        except OSError:
            data = b""
        return exc.code, data
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc).encode("ascii", "replace")


def http_call(base_url, token, method, path_qs, body=None, headers=None):
    """Returns (status, bytes). status 0 = transport-level failure.
    Retries transient Render-edge failures (404 no-server / 5xx / transport)
    with linear backoff. A 404 carrying the 'no-server' marker is treated as
    transient; a 404 from the app itself (JSON body) is returned as-is."""
    status, data = _http_once(base_url, token, method, path_qs, body, headers)
    tries = 1
    while status in _TRANSIENT and tries < _MAX_TRIES:
        # A real app 404 (unknown hash) returns a JSON body; the edge no-server
        # 404 returns plain "Not Found". Only retry the edge flavor.
        if status == 404 and b"not_found" in (data or b"").lower():
            break
        time.sleep(1.5 * tries)
        status, data = _http_once(base_url, token, method, path_qs, body, headers)
        tries += 1
    return status, data


def resp_ok(status, data):
    if not 200 <= status < 300:
        return False
    if data:
        try:
            parsed = json.loads(data.decode("utf-8", "replace"))
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                return False
        except ValueError:
            pass  # non-JSON 2xx body is accepted
    return True


def fetch_cloud_manifest(base_url, token):
    status, data = http_call(base_url, token, "GET", "/sync/manifest")
    if status == 401:
        aprint("ERROR: cloud rejected VAULT_TOKEN (HTTP 401) on "
               "%s/sync/manifest - check the token." % base_url, err=True)
        sys.exit(3)
    if not 200 <= status < 300:
        aprint("ERROR: cloud manifest fetch failed (status=%s): %s"
               % (status, data[:200].decode("ascii", "replace")), err=True)
        sys.exit(4)
    try:
        parsed = json.loads(data.decode("utf-8"))
        files = parsed["files"]
        # Accept BOTH manifest shapes (Gate 2 finding: the built cloud node
        # returns a LIST of rows [{relpath,...}, ...]; the contract sketch
        # above said a dict keyed by relpath). Normalize to the dict.
        if isinstance(files, list):
            norm = {}
            for row in files:
                rp = str(row.get("relpath") or "").strip()
                if rp:
                    norm[rp] = row
            files = norm
        if not isinstance(files, dict):
            raise ValueError("'files' is neither an object nor a list")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        aprint("ERROR: cloud manifest unparseable: %s" % exc, err=True)
        sys.exit(4)
    return files


# ---------------------------------------------------------------------------
# sync steps (live mode)
# ---------------------------------------------------------------------------

def apply_cloud_tombstones(cloud, manifest, runlog, db_updates):
    """Cloud says deleted: park our copy under _tombstoned, sha-gated."""
    for raw_rel, centry in cloud.items():
        if not centry.get("deleted"):
            continue
        rel = safe_relpath(raw_rel)
        if rel is None or rel not in manifest:
            continue
        size, sha, mtime, root = manifest[rel]
        if sha != centry.get("sha256"):
            runlog.log("conflict", rel,
                       "cloud tombstone but local sha differs - left in place")
            continue
        src = root / Path(rel.replace("/", "\\"))
        dest = unique_dest(root / Path(TOMB_REL) / Path(rel.replace("/", "\\")))
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(src), str(dest))
        except OSError as exc:
            runlog.log("error", rel, "tombstone move failed: %s" % exc)
            continue
        del manifest[rel]
        db_updates["deleted"].add(rel)
        runlog.log("tombstone", rel,
                   "cloud tombstone applied - moved to %s" % dest)


def push_files(base_url, token, cloud, manifest, runlog, db_updates):
    for rel in sorted(manifest):
        size, sha, mtime, root = manifest[rel]
        centry = cloud.get(rel)
        reason = None
        if centry is None:
            reason = "new"
        elif centry.get("deleted"):
            # sha-matching case was already parked by apply_cloud_tombstones;
            # a differing local file resurrects only if it is the newer side.
            if mtime >= float(centry.get("mtime") or 0):
                reason = "resurrect (local newer than cloud tombstone)"
            else:
                runlog.log("conflict", rel,
                           "cloud tombstone is newer - not pushing")
                continue
        elif centry.get("sha256") != sha:
            if mtime >= float(centry.get("mtime") or 0):
                reason = "changed (local mtime >= cloud)"
            else:
                runlog.log("conflict", rel,
                           "cloud copy is newer - not pushing older local")
                continue
        else:
            continue  # identical
        src = root / Path(rel.replace("/", "\\"))
        try:
            with open(src, "rb") as f:
                body = f.read()
        except OSError as exc:
            runlog.log("error", rel, "push read failed: %s" % exc)
            continue
        status, data = http_call(
            base_url, token, "POST", "/sync/file", body=body,
            headers={"X-Relpath": rel, "X-Sha256": sha,
                     "X-Mtime": repr(mtime),
                     "Content-Type": "application/octet-stream"})
        if resp_ok(status, data):
            runlog.log("push", rel, "ok (%s, %d bytes)" % (reason, size))
        else:
            runlog.log("error", rel, "push rejected (status=%s)" % status)


def pull_files(base_url, token, cloud, manifest, history, roots,
               skip_prefixes_normed, runlog, db_updates):
    """Pull genuinely cloud-new files into <root>\\99 Inbox\\from-cloud."""
    primary = roots[0]
    for raw_rel, centry in cloud.items():
        if centry.get("deleted"):
            continue
        rel = safe_relpath(raw_rel)
        if rel is None:
            runlog.log("error", raw_rel, "unsafe cloud relpath - pull refused")
            continue
        if rel in manifest:
            continue  # we hold it; push logic owns this pair
        pat = blocked_pattern_path(rel)
        if pat:
            runlog.log("skip-blocklist", rel,
                       "cloud file hits blocklist pattern %s - not pulled" % pat)
            continue
        if skip_prefix_hit(norm_rel(rel), skip_prefixes_normed):
            continue  # inside an excluded prefix (incl. the inbox itself)
        if rel in history:
            continue  # known before (possibly locally deleted) - not cloud-new
        win_rel = Path(rel.replace("/", "\\"))
        if any((r / win_rel).exists() for r in roots):
            continue  # exists on disk but was skipped from the walk
        landing = primary / Path(INBOX_REL) / win_rel
        if landing.exists():
            runlog.log("conflict", rel, "landing path already exists - "
                       "pull skipped")
            continue
        want_sha = centry.get("sha256") or ""
        status, data = http_call(
            base_url, token, "GET",
            "/sync/file?h=" + urllib.parse.quote(want_sha))
        if not 200 <= status < 300:
            runlog.log("error", rel, "pull download failed (status=%s)" % status)
            continue
        got_sha = hashlib.sha256(data).hexdigest()
        if got_sha != want_sha:
            runlog.log("error", rel,
                       "pull sha mismatch (want %s got %s) - NOT written"
                       % (want_sha[:12], got_sha[:12]))
            continue
        mtime = float(centry.get("mtime") or time.time())
        try:
            atomic_write(landing, data, mtime)
        except OSError as exc:
            runlog.log("error", rel, "pull write failed: %s" % exc)
            continue
        db_updates["pulled"][rel] = (len(data), want_sha, mtime)
        runlog.log("pull", rel, "ok -> %s" % landing)


def push_tombstones(base_url, token, manifest, history, roots, runlog,
                    db_updates):
    """Local file known from a previous run is now gone -> tell the cloud."""
    primary = roots[0]
    for rel, h in sorted(history.items()):
        if h["deleted"]:
            continue
        if rel in manifest:
            continue
        if blocked_pattern_path(rel):
            continue  # blocklisted paths are never synced in any direction
        win_rel = Path(rel.replace("/", "\\"))
        if any((r / win_rel).exists() for r in roots):
            # still on disk - it just was not walkable (junction/skip rule);
            # absence from the manifest is not a deletion.
            continue
        if (primary / Path(INBOX_REL) / win_rel).exists() or \
           (primary / Path(TOMB_REL) / win_rel).exists():
            continue  # it lives in the pull inbox / tombstone parking lot
        status, data = http_call(
            base_url, token, "POST", "/sync/tombstone",
            body=json.dumps({"relpath": rel}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        if resp_ok(status, data):
            db_updates["deleted"].add(rel)
            runlog.log("tombstone", rel, "ok (local file gone)")
        else:
            runlog.log("error", rel, "tombstone rejected (status=%s)" % status)


def write_end_state(con, manifest, db_updates):
    for rel, (size, sha, mtime, _root) in manifest.items():
        con.execute(
            "INSERT INTO files(relpath, size, sha256, mtime, deleted) "
            "VALUES (?,?,?,?,0) ON CONFLICT(relpath) DO UPDATE SET "
            "size=excluded.size, sha256=excluded.sha256, "
            "mtime=excluded.mtime, deleted=0", (rel, size, sha, mtime))
    for rel, (size, sha, mtime) in db_updates["pulled"].items():
        con.execute(
            "INSERT INTO files(relpath, size, sha256, mtime, deleted) "
            "VALUES (?,?,?,?,0) ON CONFLICT(relpath) DO UPDATE SET "
            "size=excluded.size, sha256=excluded.sha256, "
            "mtime=excluded.mtime, deleted=0", (rel, size, sha, mtime))
    for rel in db_updates["deleted"]:
        con.execute(
            "INSERT INTO files(relpath, size, sha256, mtime, deleted) "
            "VALUES (?,NULL,NULL,NULL,1) ON CONFLICT(relpath) DO UPDATE SET "
            "deleted=1", (rel,))
    con.commit()


# ---------------------------------------------------------------------------
# dry-run planning (no network, no writes)
# ---------------------------------------------------------------------------

def plan_dry_run(manifest, history, roots, runlog):
    primary = roots[0]
    for rel in sorted(manifest):
        size, sha, mtime, _root = manifest[rel]
        h = history.get(rel)
        if h is None:
            runlog.log("push", rel, "would push: new since last recorded run")
        elif h["sha256"] != sha:
            runlog.log("push", rel, "would push: content changed")
        # cloud-side comparison is impossible offline; identical-to-history
        # files are assumed already synced
    for rel, h in sorted(history.items()):
        if h["deleted"] or rel in manifest:
            continue
        if blocked_pattern_path(rel):
            continue
        win_rel = Path(rel.replace("/", "\\"))
        if any((r / win_rel).exists() for r in roots):
            continue  # still on disk, just not walkable (junction/skip rule)
        if (primary / Path(INBOX_REL) / win_rel).exists() or \
           (primary / Path(TOMB_REL) / win_rel).exists():
            continue  # lives in the pull inbox / tombstone parking lot
        runlog.log("tombstone", rel, "would tombstone: gone from disk")
    aprint("DRY-RUN note: pull candidates unknown offline (cloud manifest "
           "not fetched); push/tombstone plan is vs local DB history only.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv):
    dry_run = False
    for arg in argv:
        if arg == "--dry-run":
            dry_run = True
        elif arg == "--once":
            pass  # the normal single pass; default behaviour
        elif arg in ("-h", "--help"):
            aprint(__doc__)
            return 0
        else:
            aprint("ERROR: unknown argument %r (use --once or --dry-run)"
                   % arg, err=True)
            return 2
    base_url, token = load_config()

    allow_lines = load_list_file(ALLOW_FILE, DEFAULT_ALLOW_TEXT, dry_run)
    skip_lines = load_list_file(SKIP_FILE, DEFAULT_SKIP_TEXT, dry_run)
    skip_normed = [norm_rel(s) for s in skip_lines if norm_rel(s)]

    roots = []
    for line in allow_lines:
        root = Path(line)
        if not root.is_absolute():
            aprint("ERROR: allow root %r is not absolute - ignored" % line,
                   err=True)
            continue
        roots.append(root)
    if not roots:
        aprint("ERROR: no usable allow roots in %s" % ALLOW_FILE, err=True)
        return 2

    con = open_db(dry_run)
    runlog = RunLog(con, dry_run)
    history = load_history(con)

    manifest = {}
    for root in roots:
        if not root.is_dir():
            runlog.log("error", str(root), "allow root missing on disk")
            continue
        walk_root(root, skip_normed, runlog, manifest)

    if dry_run:
        plan_dry_run(manifest, history, roots, runlog)
        aprint("DRY-RUN summary: pushed %d, pulled %d, tombstoned %d, "
               "skipped %d junctions / %d blocklist, errors %d, conflicts %d"
               % (runlog.n("push"), runlog.n("pull"), runlog.n("tombstone"),
                  runlog.n("skip-junction"), runlog.n("skip-blocklist"),
                  runlog.n("error"), runlog.n("conflict")))
        if con is not None:
            con.close()
        return 0

    cloud = fetch_cloud_manifest(base_url, token)

    db_updates = {"deleted": set(), "pulled": {}}
    apply_cloud_tombstones(cloud, manifest, runlog, db_updates)
    push_files(base_url, token, cloud, manifest, runlog, db_updates)
    pull_files(base_url, token, cloud, manifest, history, roots,
               skip_normed, runlog, db_updates)
    push_tombstones(base_url, token, manifest, history, roots, runlog,
                    db_updates)
    write_end_state(con, manifest, db_updates)

    aprint("pushed %d, pulled %d, tombstoned %d, skipped %d junctions / "
           "%d blocklist, errors %d, conflicts %d"
           % (runlog.n("push"), runlog.n("pull"), runlog.n("tombstone"),
              runlog.n("skip-junction"), runlog.n("skip-blocklist"),
              runlog.n("error"), runlog.n("conflict")))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


# ---------------------------------------------------------------------------
# Task Scheduler registration (every 5 minutes, as the current user).
# PowerShell-safe one-liner -- NOTE: the deploy copy location may differ from
# this worktree; point /tr at wherever the script actually lives (for
# example the C:\Users\sam\cenas-vault\ runtime copy):
#
#   schtasks /create /f /tn "Cenas\Vault-Sync" /sc minute /mo 5 /tr "python C:\Users\sam\cenas-vault\vault_sync.py --once"
#
# Verify:   schtasks /query /tn "Cenas\Vault-Sync"
# Run now:  schtasks /run /tn "Cenas\Vault-Sync"
# Remove:   schtasks /delete /f /tn "Cenas\Vault-Sync"
# ---------------------------------------------------------------------------
