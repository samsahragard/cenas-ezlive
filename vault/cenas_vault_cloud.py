#!/usr/bin/env python3
# ============================================================================
#  CENAS VAULT CLOUD - Hosted File Portal (Render fork of cenas_vault.py)
#  Single file. Stdlib only. No installs, no dependencies.
#
#  Cloud fork of the local Cenas Vault portal. Same UI, search index, and
#  meta/links logic as the local app, with these changes:
#    - Env-only config: PORT, VAULT_ROOT, VAULT_DB, VAULT_DATA_JSON,
#      VAULT_TOKEN (required). Binds 0.0.0.0.
#    - HTTP Basic auth (user "sam", password VAULT_TOKEN) on EVERY route.
#    - /api/open and /api/reveal are disabled (no OS shell in the cloud).
#    - /sync/manifest, /sync/file (GET+POST), /sync/tombstone endpoints
#      backed by sqlite3 at VAULT_DB so a local agent can mirror files up.
#    - Persistence (cenas_vault_data.json, kb.json) lives on the persistent
#      disk (/var/data), never beside the script (repo dir is ephemeral).
#
#  RUN
#    VAULT_TOKEN=... python cenas_vault_cloud.py    -> http://0.0.0.0:10000
# ============================================================================

import base64
import fnmatch
import hashlib
import hmac
import json
import mimetypes
import os
import platform
import sqlite3
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Env-only config (Render injects PORT; disk is mounted at /var/data). --
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT") or 10000)
VAULT_ROOT = os.environ.get("VAULT_ROOT") or "/var/data/vault"
VAULT_DB = os.environ.get("VAULT_DB") or "/var/data/vault_sync.db"
VAULT_DATA_JSON = os.environ.get("VAULT_DATA_JSON") or "/var/data/cenas_vault_data.json"
VAULT_TOKEN = os.environ.get("VAULT_TOKEN") or ""
if not VAULT_TOKEN:
    print("ERROR: VAULT_TOKEN environment variable is required (Basic auth password).")
    sys.exit(2)

AUTH_USER = b"sam"
AUTH_PASS = VAULT_TOKEN.encode("utf-8")

os.makedirs(VAULT_ROOT, exist_ok=True)

MACHINE = "CLOUD"
MIRROR = False

# --- The folder this portal manages. No home-dir fallback in the cloud. ----
ROOTS = [("Cenas", VAULT_ROOT)]

# --- Seeded into the sidebar on first run. Editable in the UI afterward. ---
DEFAULT_LINKS = [
    {"label": "Live \u2014 app.cenaskitchen.com", "url": "https://app.cenaskitchen.com"},
    {"label": "GitHub \u2014 cenas-ezlive", "url": "https://github.com/samsahragard/cenas-ezlive"},
    {"label": "Render dashboard", "url": "https://dashboard.render.com"},
    {"label": "Toast", "url": "https://www.toasttab.com"},
]

# --- Folders the search index skips (still visible when browsing). --------
IGNORE_DIRS = {
    "node_modules", "__pycache__", ".git", ".hg", ".svn", ".venv", "venv",
    "env", ".idea", ".vscode", ".codex", "dist", "build", ".next", ".cache",
    "site-packages", "$recycle.bin", "system volume information",
    "appdata", ".gradle", ".npm", ".nuget", ".android",
}

MAX_INDEX = 200_000          # safety cap on indexed entries
LIST_CAP = 3000              # max entries returned for one folder
PREVIEW_BYTES = 120_000      # text preview size
RAW_CAP = 60 * 1024 * 1024   # max file size served for preview/thumbnails

TEXT_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".md", ".txt", ".html",
    ".htm", ".css", ".csv", ".tsv", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".conf", ".log", ".sql", ".sh", ".bat", ".ps1", ".xml", ".env", ".gitignore",
}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp"}

IS_WIN = platform.system() == "Windows"
# Persistence lives on the persistent disk, NOT beside the script: the repo
# checkout on Render is wiped on every deploy.
DATA_FILE = VAULT_DATA_JSON
KB_FILE = os.path.join(os.path.dirname(VAULT_DATA_JSON) or ".", "kb.json")

# ---------------------------------------------------------------------------
# Roots resolution (no home-dir fallback in the cloud build)
# ---------------------------------------------------------------------------
def _resolve_roots():
    out = []
    for label, path in ROOTS:
        rp = os.path.realpath(os.path.expanduser(path))
        if os.path.isdir(rp):
            out.append({"label": label, "path": rp})
        else:
            print(f"  [skip] root not found: {label} -> {path}")
    return out

ROOTS_RESOLVED = _resolve_roots()
_ROOT_KEYS = [os.path.normcase(r["path"]) for r in ROOTS_RESOLVED]

def safe_path(p):
    """Resolve p and confirm it lives inside one of the configured roots."""
    if not p:
        return None
    rp = os.path.realpath(p)
    key = os.path.normcase(rp)
    for rk in _ROOT_KEYS:
        if key == rk or key.startswith(rk + os.sep):
            return rp
    return None

# ---------------------------------------------------------------------------
# Persisted metadata: pins, tags, notes, links
# ---------------------------------------------------------------------------
_META_LOCK = threading.Lock()

def _load_meta():
    meta = {"pins": {}, "tags": {}, "notes": {}, "links": None}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k in ("pins", "tags", "notes"):
            if isinstance(saved.get(k), dict):
                meta[k] = saved[k]
        if isinstance(saved.get("links"), list):
            meta["links"] = saved["links"]
    except Exception:
        pass
    if meta["links"] is None:
        meta["links"] = [dict(x) for x in DEFAULT_LINKS]
    return meta

META = _load_meta()

def _save_meta():
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(META, f, indent=2)
        os.replace(tmp, DATA_FILE)
        _META_MTIME[0] = os.path.getmtime(DATA_FILE)
    except Exception as e:
        print(f"  [warn] could not save data file: {e}")
    queue_sync()

def _mkey(p):
    return os.path.normcase(os.path.abspath(p))

def meta_for(p):
    k = _mkey(p)
    return {
        "pinned": k in META["pins"],
        "tags": META["tags"].get(k, []),
        "note": META["notes"].get(k, ""),
    }

# ---------------------------------------------------------------------------
# Knowledge base (kb.json) - the centralized app database, synced to AiCk
# ---------------------------------------------------------------------------
KB_LOCK = threading.Lock()
_KB_MTIME = [0.0]
_META_MTIME = [0.0]

def _load_kb():
    try:
        with open(KB_FILE, "r", encoding="utf-8") as f:
            kb = json.load(f)
        if isinstance(kb, dict) and isinstance(kb.get("categories"), list):
            return kb
    except Exception:
        pass
    return {"version": 1, "categories": []}

KB = _load_kb()

def _kb_fresh():
    """Reload kb.json if it changed on disk (the mirror receives scp pushes)."""
    global KB
    try:
        m = os.path.getmtime(KB_FILE)
    except OSError:
        return KB
    if m > _KB_MTIME[0]:
        with KB_LOCK:
            KB = _load_kb()
            _KB_MTIME[0] = m
    return KB

def _meta_fresh():
    global META
    try:
        m = os.path.getmtime(DATA_FILE)
    except OSError:
        return
    if m > _META_MTIME[0]:
        with _META_LOCK:
            META = _load_meta()
        _META_MTIME[0] = m

def _save_kb():
    tmp = KB_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(KB, f, indent=1)
        os.replace(tmp, KB_FILE)
        _KB_MTIME[0] = os.path.getmtime(KB_FILE)
    except Exception as e:
        print(f"  [warn] could not save kb file: {e}")
    queue_sync()

def _kb_walk(nodes):
    for n in nodes:
        yield n
        yield from _kb_walk(n.get("children") or [])

def _kb_find(nid):
    for n in _kb_walk(KB.get("categories") or []):
        if n.get("id") == nid:
            return n
    return None

# ---------------------------------------------------------------------------
# Mirror sync: not applicable in the cloud build. The cloud node RECEIVES
# content through the authenticated /sync/* endpoints below; it never pushes
# over ssh/scp. The stubs keep the UI's sync wiring harmless.
# ---------------------------------------------------------------------------
SYNC_STATUS = {"last": 0.0, "ok": None, "msg": "cloud node - no AiCk push", "running": False}

def sync_to_aick():
    return dict(SYNC_STATUS)

def queue_sync(delay=6.0):
    return

# ---------------------------------------------------------------------------
# Sync store: sqlite3 state for the /sync/* endpoints. A local agent pushes
# file bytes up; this DB is the manifest of what the cloud copy holds.
# ---------------------------------------------------------------------------
# Relpaths whose ANY path segment matches one of these (fnmatch,
# case-insensitive) are refused. Sanitize-by-construction: agent memory,
# conversations, env/credential/secret files never land on the cloud disk.
SYNC_BLOCKLIST = [
    "tools.md", "memory.md", "memory", "state", "*conversations*",
    "*.env", "*credentials*", "*secrets*", ".git", "cenas_vault_data.json",
]

def db_connect():
    return sqlite3.connect(VAULT_DB, timeout=30)

def db_init():
    parent = os.path.dirname(VAULT_DB)
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

def blocked_relpath(rel):
    for seg in rel.replace("\\", "/").split("/"):
        s = seg.strip().lower()
        if not s:
            continue
        for pat in SYNC_BLOCKLIST:
            if fnmatch.fnmatchcase(s, pat):
                return True
    return False

def sync_dest(relpath):
    """Normalize a forward-slash relpath; resolve it inside VAULT_ROOT.
    Returns (clean_relpath, absolute_dest) or (None, None) if it escapes."""
    rel = str(relpath or "").replace("\\", "/").strip().strip("/")
    if not rel:
        return None, None
    parts = rel.split("/")
    if any(p in ("", ".", "..") or ":" in p for p in parts):
        return None, None
    dest = safe_path(os.path.join(VAULT_ROOT, *parts))
    if not dest:
        return None, None
    return "/".join(parts), dest

def quarantine(dest, rel):
    """Move an existing file aside instead of destroying it."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    qpath = os.path.join(VAULT_ROOT, "quarantine", stamp, *rel.split("/"))
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
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

# ---------------------------------------------------------------------------
# Search index
# ---------------------------------------------------------------------------
INDEX = []
INDEX_STATUS = {"building": False, "count": 0, "finished": 0.0, "truncated": False}

def _walk_root(root, out):
    label, base = root["label"], root["path"]
    stack = [base]
    while stack:
        if len(out) >= MAX_INDEX:
            INDEX_STATUS["truncated"] = True
            return
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if len(out) >= MAX_INDEX:
                    INDEX_STATUS["truncated"] = True
                    return
                name = e.name
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if name.lower() in IGNORE_DIRS or name.startswith("."):
                        continue
                    try:
                        m = e.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        m = 0.0
                    out.append({"p": e.path, "n": name, "nl": name.lower(),
                                "pl": e.path.lower(), "d": True, "s": 0,
                                "m": m, "r": label})
                    stack.append(e.path)
                else:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    out.append({"p": e.path, "n": name, "nl": name.lower(),
                                "pl": e.path.lower(), "d": False,
                                "s": st.st_size, "m": st.st_mtime, "r": label})

def _build_index():
    global INDEX
    INDEX_STATUS["building"] = True
    INDEX_STATUS["truncated"] = False
    fresh = []
    for root in ROOTS_RESOLVED:
        _walk_root(root, fresh)
        INDEX_STATUS["count"] = len(fresh)
    INDEX = fresh
    INDEX_STATUS["count"] = len(fresh)
    INDEX_STATUS["building"] = False
    INDEX_STATUS["finished"] = time.time()

def reindex_async():
    if INDEX_STATUS["building"]:
        return
    threading.Thread(target=_build_index, daemon=True).start()

_SEPS = (" ", "-", "_", ".", os.sep)

def search_index(q, limit=80):
    ql = q.lower().strip()
    if not ql:
        return []
    hits = []
    for it in INDEX:
        nl = it["nl"]
        if ql == nl:
            score = 100
        elif nl.startswith(ql):
            score = 85
        else:
            pos = nl.find(ql)
            if pos > 0 and nl[pos - 1] in _SEPS:
                score = 65
            elif pos > 0:
                score = 45
            elif ql in it["pl"]:
                score = 22
            else:
                continue
        if it["d"]:
            score += 4
        if _mkey(it["p"]) in META["pins"]:
            score += 20
        hits.append((score, it))
    hits.sort(key=lambda h: (-h[0], -h[1]["m"]))
    return [_entry(h[1]) for h in hits[:limit]]

def recent_files(limit=100):
    files = [it for it in INDEX if not it["d"]]
    files.sort(key=lambda x: -x["m"])
    return [_entry(it) for it in files[:limit]]

def tagged_entries(tag):
    out = []
    for key, tags in META["tags"].items():
        if tag in tags and os.path.exists(key):
            out.append(_stat_entry(key))
    out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return out

def _entry(it):
    e = {"name": it["n"], "path": it["p"], "dir": it["d"], "size": it["s"],
         "mtime": it["m"], "root": it["r"]}
    e.update(meta_for(it["p"]))
    return e

def _stat_entry(path):
    try:
        st = os.stat(path)
        is_dir = os.path.isdir(path)
        e = {"name": os.path.basename(path) or path, "path": path,
             "dir": is_dir, "size": 0 if is_dir else st.st_size,
             "mtime": st.st_mtime, "root": "", "missing": False}
    except OSError:
        e = {"name": os.path.basename(path) or path, "path": path, "dir": False,
             "size": 0, "mtime": 0, "root": "", "missing": True}
    e.update(meta_for(path))
    return e

def list_dir(path):
    entries, truncated = [], False
    try:
        it = os.scandir(path)
    except OSError as exc:
        return None, str(exc)
    with it:
        for e in it:
            if len(entries) >= LIST_CAP:
                truncated = True
                break
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            ent = {"name": e.name, "path": e.path, "dir": is_dir,
                   "size": 0 if is_dir else st.st_size, "mtime": st.st_mtime}
            ent.update(meta_for(e.path))
            entries.append(ent)
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"entries": entries, "truncated": truncated}, None

# ---------------------------------------------------------------------------
# OS actions: removed in the cloud build. /api/open and /api/reveal answer
# 400 {"ok": false, "error": "local-only"} instead of shelling out.
# ---------------------------------------------------------------------------

# ===========================================================================
#  UI - served at /
# ===========================================================================
HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cenas Vault</title>
<style>
:root{
  --bg:#0e1311;        /* vault graphite */
  --panel:#141b18;     /* raised surface */
  --panel2:#19211d;    /* hover surface  */
  --line:#243029;      /* hairline       */
  --ink:#e8efea;       /* primary text   */
  --ash:#8ca096;       /* secondary text */
  --dim:#5c6e65;       /* tertiary text  */
  --jade:#43d9a3;      /* accent         */
  --jade-dim:#2a8f6c;
  --amber:#e3b341;     /* busy / warning */
  --red:#e36a5f;
  --mono:"Cascadia Mono","Consolas",ui-monospace,"SF Mono",Menlo,monospace;
  --ui:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:var(--bg);color:var(--ink);font:13px/1.45 var(--ui);
  display:grid;grid-template-rows:52px 1fr 30px;
  grid-template-columns:236px minmax(0,1fr) auto;
  grid-template-areas:"top top top" "rail main detail" "foot foot foot";
  overflow:hidden;
}
::selection{background:rgba(67,217,163,.25)}
button{font:inherit;color:inherit;background:none;border:none;cursor:pointer}
input,textarea,select{font:inherit;color:var(--ink);background:var(--panel);
  border:1px solid var(--line);border-radius:2px;outline:none}
input:focus-visible,textarea:focus-visible,select:focus-visible,
button:focus-visible,[tabindex]:focus-visible{outline:1px solid var(--jade);outline-offset:1px}
a{color:var(--jade);text-decoration:none}
a:hover{text-decoration:underline}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#26312b;border:2px solid var(--bg);border-radius:6px}
::-webkit-scrollbar-track{background:transparent}
.eyebrow{font-size:10.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--dim);font-weight:600}

/* ---------- top bar ---------- */
#top{grid-area:top;display:flex;align-items:center;gap:16px;
  border-bottom:1px solid var(--line);padding:0 14px;background:var(--bg)}
#brand{display:flex;align-items:center;gap:10px;min-width:212px}
#brand .glyph{width:22px;height:22px;border:1px solid var(--jade);border-radius:2px;
  display:grid;place-items:center;color:var(--jade);font:600 11px var(--mono)}
#brand .name{font-weight:650;letter-spacing:.08em;font-size:13px}
#brand .sub{display:block;font-size:9.5px;letter-spacing:.18em;color:var(--dim);font-weight:500}
#searchbox{flex:1;max-width:560px;position:relative}
#searchbox input{width:100%;padding:7px 64px 7px 30px;background:var(--panel);cursor:pointer}
#searchbox .lens{position:absolute;left:10px;top:8px;color:var(--dim)}
#searchbox kbd{position:absolute;right:8px;top:6px;font:10.5px var(--mono);color:var(--dim);
  border:1px solid var(--line);border-radius:2px;padding:2px 5px;background:var(--bg)}
#topright{margin-left:auto;display:flex;align-items:center;gap:10px}
#idxpill{display:flex;align-items:center;gap:7px;font:11px var(--mono);color:var(--ash);
  border:1px solid var(--line);border-radius:2px;padding:4px 9px;background:var(--panel)}
#idxpill .dot{width:7px;height:7px;border-radius:50%;background:var(--jade)}
#idxpill.busy .dot{background:var(--amber);animation:pulse 1.1s infinite}
@keyframes pulse{50%{opacity:.35}}
.tbtn{border:1px solid var(--line);border-radius:2px;padding:5px 10px;color:var(--ash);background:var(--panel)}
.tbtn:hover{color:var(--ink);background:var(--panel2)}

/* ---------- left rail ---------- */
#rail{grid-area:rail;border-right:1px solid var(--line);overflow-y:auto;padding:14px 0 20px}
.rsec{padding:0 14px;margin-bottom:18px}
.rsec .eyebrow{display:flex;justify-content:space-between;align-items:center;margin-bottom:7px}
.rsec .eyebrow button{color:var(--dim);font-size:14px;line-height:1}
.rsec .eyebrow button:hover{color:var(--jade)}
.ritem{display:flex;align-items:center;gap:8px;width:100%;text-align:left;
  padding:5px 8px;margin:0 -8px;border-radius:2px;color:var(--ash);
  white-space:nowrap;overflow:hidden}
.ritem:hover{background:var(--panel);color:var(--ink)}
.ritem.active{background:var(--panel);color:var(--ink);box-shadow:inset 2px 0 0 var(--jade)}
.ritem .lab{overflow:hidden;text-overflow:ellipsis}
.ritem .ic{color:var(--dim);width:14px;flex:none;text-align:center}
.ritem .ic.star{color:var(--jade)}
.tagchip{display:inline-flex;align-items:center;gap:5px;margin:0 6px 6px 0;
  border:1px solid var(--line);border-radius:2px;padding:2px 8px;
  font:11px var(--mono);color:var(--ash)}
.tagchip:hover{border-color:var(--jade-dim);color:var(--ink)}
.tagchip .ct{color:var(--dim)}
.linkrow{display:flex;align-items:center;gap:6px}
.linkrow .ritem{flex:1}
.linkrow .rm{visibility:hidden;color:var(--dim);padding:2px 4px}
.linkrow:hover .rm{visibility:visible}
.linkrow .rm:hover{color:var(--red)}
#linkform{display:none;margin-top:6px;gap:5px;flex-direction:column}
#linkform.show{display:flex}
#linkform input{padding:5px 8px;font-size:12px}
#linkform .row{display:flex;gap:6px}
.minibtn{border:1px solid var(--line);border-radius:2px;padding:4px 9px;
  font-size:11px;color:var(--ash);background:var(--panel)}
.minibtn:hover{color:var(--ink);border-color:var(--jade-dim)}
.railnote{color:var(--dim);font-size:11.5px;padding:2px 0}

/* ---------- main ---------- */
#main{grid-area:main;display:flex;flex-direction:column;min-width:0;overflow:hidden}
#pathline{padding:20px 22px 6px;font-family:var(--mono);
  font-size:clamp(16px,2vw,23px);font-weight:500;letter-spacing:-.01em;
  white-space:nowrap;overflow-x:auto;scrollbar-width:none}
#pathline::-webkit-scrollbar{display:none}
#pathline .seg{cursor:pointer;color:var(--ink)}
#pathline .seg:hover{text-decoration:underline;text-underline-offset:4px}
#pathline .seg.rootseg{color:var(--jade)}
#pathline .sep{color:var(--dim);margin:0 9px;font-size:.78em}
#pathline .view{color:var(--ink)}
#pathline .viewq{color:var(--jade)}
#metarow{display:flex;align-items:center;gap:14px;padding:4px 22px 12px;
  border-bottom:1px solid var(--line);color:var(--ash);font-size:12px;min-height:34px}
#metarow .up{color:var(--ash);border:1px solid var(--line);border-radius:2px;padding:2px 9px}
#metarow .up:hover{color:var(--ink);background:var(--panel)}
#metarow .spacer{flex:1}
.vtoggle{display:flex;border:1px solid var(--line);border-radius:2px;overflow:hidden}
.vtoggle button{padding:3px 10px;color:var(--dim);font-size:11.5px}
.vtoggle button.on{background:var(--panel2);color:var(--jade)}
#listing{flex:1;overflow-y:auto;padding-bottom:30px}

/* list view */
.lhead,.lrow{display:grid;align-items:center;
  grid-template-columns:minmax(0,1fr) 200px 86px 120px 168px;
  padding:0 22px;column-gap:14px}
.lhead{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
  z-index:2;height:30px}
.lhead button{text-align:left;color:var(--dim);font-size:10.5px;letter-spacing:.14em;
  text-transform:uppercase;font-weight:600;padding:0}
.lhead button:hover{color:var(--ash)}
.lhead button.on{color:var(--jade)}
.lrow{height:34px;border-bottom:1px solid rgba(36,48,41,.45);cursor:default;user-select:none}
.lrow:hover{background:var(--panel)}
.lrow.sel{background:var(--panel);box-shadow:inset 2px 0 0 var(--jade)}
.lrow .nm{display:flex;align-items:center;gap:10px;min-width:0;color:var(--ink)}
.lrow .nm .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lrow.miss .nm .t{color:var(--dim);text-decoration:line-through}
.badge{flex:none;width:34px;height:18px;border:1px solid var(--line);border-radius:2px;
  display:grid;place-items:center;font:600 8.5px/1 var(--mono);color:var(--ash);
  letter-spacing:.06em;background:var(--panel)}
.badge.dir{border-color:var(--jade-dim);color:var(--jade)}
.pinstar{flex:none;color:var(--dim);width:16px;text-align:center}
.pinstar.on{color:var(--jade)}
.pinstar:hover{color:var(--jade)}
.lrow .tg{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;color:var(--dim);
  font:11px var(--mono)}
.lrow .tg b{color:var(--ash);font-weight:500}
.lrow .sz,.lrow .mt{font:11.5px var(--mono);color:var(--ash);text-align:right}
.lrow .mt{text-align:left}
.lrow .acts{display:flex;gap:4px;justify-content:flex-end;visibility:hidden}
.lrow:hover .acts,.lrow.sel .acts{visibility:visible}
.acts button{font-size:11px;color:var(--ash);border:1px solid var(--line);
  border-radius:2px;padding:2px 7px;background:var(--bg)}
.acts button:hover{color:var(--ink);border-color:var(--jade-dim)}

/* grid view */
#listing.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
  gap:12px;padding:16px 22px;align-content:start}
.card{border:1px solid var(--line);border-radius:2px;background:var(--panel);
  padding:10px;cursor:default;min-width:0}
.card:hover{background:var(--panel2)}
.card.sel{border-color:var(--jade);box-shadow:inset 0 2px 0 var(--jade)}
.card .thumb{height:84px;display:grid;place-items:center;border:1px solid var(--line);
  border-radius:2px;margin-bottom:8px;background:var(--bg);overflow:hidden}
.card .thumb img{max-width:100%;max-height:100%;object-fit:contain}
.card .thumb .badge{width:46px;height:24px;font-size:10px}
.card .t{font-size:12px;color:var(--ink);overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap}
.card .s{font:10.5px var(--mono);color:var(--dim);margin-top:3px}

.emptymsg{padding:46px 22px;color:var(--dim);font-size:13px}
.emptymsg .big{font-size:15px;color:var(--ash);margin-bottom:6px}

/* ---------- detail panel ---------- */
#detail{grid-area:detail;width:320px;border-left:1px solid var(--line);
  overflow-y:auto;padding:18px 16px 30px;display:none}
#detail.show{display:block}
#detail .close{float:right;color:var(--dim);font-size:15px;padding:0 4px}
#detail .close:hover{color:var(--ink)}
#dname{font-size:15px;font-weight:600;margin:2px 0 10px;word-break:break-word;
  padding-right:24px}
#dpath{font:11px/1.6 var(--mono);color:var(--ash);word-break:break-all;
  border:1px solid var(--line);border-radius:2px;padding:8px;background:var(--panel);
  cursor:pointer}
#dpath:hover{border-color:var(--jade-dim)}
.dgrid{display:grid;grid-template-columns:auto 1fr;gap:5px 14px;margin:12px 0;
  font-size:12px}
.dgrid .k{color:var(--dim)}
.dgrid .v{font-family:var(--mono);font-size:11.5px;color:var(--ash)}
#dactions{display:flex;gap:6px;margin:4px 0 16px;flex-wrap:wrap}
#dactions .minibtn.primary{border-color:var(--jade-dim);color:var(--jade)}
#dactions .minibtn.primary:hover{background:rgba(67,217,163,.08)}
.dsec{margin-bottom:16px}
.dsec .eyebrow{margin-bottom:6px}
#dtags{width:100%;padding:6px 8px;font:11.5px var(--mono)}
#dnote{width:100%;min-height:74px;padding:7px 8px;resize:vertical;font-size:12px;
  line-height:1.5}
.dhint{color:var(--dim);font-size:10.5px;margin-top:4px}
#dpreview{border:1px solid var(--line);border-radius:2px;background:var(--panel);
  overflow:hidden}
#dpreview img{display:block;max-width:100%}
#dpreview pre{font:10.5px/1.55 var(--mono);color:var(--ash);padding:10px;
  max-height:300px;overflow:auto;white-space:pre-wrap;word-break:break-word}
#dpreview .none{padding:14px;color:var(--dim);font-size:11.5px}
.flash{color:var(--jade);font-size:11px;margin-left:8px;opacity:0;transition:opacity .15s}
.flash.on{opacity:1}

/* ---------- footer ---------- */
#foot{grid-area:foot;display:flex;align-items:center;gap:18px;
  border-top:1px solid var(--line);padding:0 14px;font:10.5px var(--mono);
  color:var(--dim);background:var(--bg)}
#foot .dot{width:6px;height:6px;border-radius:50%;background:var(--jade);
  display:inline-block;margin-right:6px}
#foot.busy .dot{background:var(--amber)}
#foot .right{margin-left:auto}

/* ---------- knowledge base ---------- */
#listing.kbwrap{padding:18px 22px 60px;overflow-y:auto;display:block}
.kbb{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:2px;
  padding:2px 8px;font:600 9.5px var(--mono);letter-spacing:.1em;color:var(--ash);margin:0 6px 4px 0}
.kbb.ck{border-color:var(--jade-dim);color:var(--jade)}
.kbb.aick{border-color:#7aa2e3;color:#7aa2e3}
.kbb.stale{border-color:var(--red);color:var(--red)}
.kbb.hold{border-color:var(--amber);color:var(--amber)}
.kbb.soon{color:var(--dim)}
.kbb.unknown{color:var(--dim);border-style:dashed}
.kbdesc{color:var(--ink);font-size:13.5px;max-width:880px;margin:10px 0 4px;line-height:1.55}
.kbhow{color:var(--ash);font-size:12.5px;line-height:1.65;max-width:880px;margin:6px 0 4px;white-space:pre-wrap}
.kbgrid{display:grid;grid-template-columns:110px 1fr;gap:6px 14px;font-size:12px;max-width:880px;margin:12px 0}
.kbgrid .k{color:var(--dim)}
.kbgrid .v{font-family:var(--mono);font-size:11.5px;color:var(--ash);word-break:break-all;line-height:1.6}
.kbsec{margin:18px 0 0;max-width:980px}
.kbsec>.eyebrow{margin-bottom:8px}
.kblink{margin-bottom:5px;font-size:12.5px}
.kblink .u{color:var(--dim);font:10.5px var(--mono);margin-left:8px;word-break:break-all}
.kbpath{border:1px solid var(--line);border-radius:2px;background:var(--panel);margin-bottom:8px}
.kbpath .hd{display:flex;align-items:center;gap:10px;padding:8px 10px;flex-wrap:wrap}
.kbpath .pp{font:11.5px var(--mono);color:var(--ink);word-break:break-all;flex:1;min-width:200px}
.kbpath .pl{color:var(--dim);font-size:11px}
.kbpane{border-top:1px solid var(--line);max-height:340px;overflow-y:auto;background:var(--bg)}
.kbrow{display:flex;align-items:center;gap:10px;padding:5px 12px;border-bottom:1px solid rgba(36,48,41,.45)}
.kbrow .t{color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;cursor:pointer}
.kbrow .t:hover{color:var(--jade)}
.kbrow .z{font:10.5px var(--mono);color:var(--dim);flex:none}
.kbcrumb{padding:7px 12px;font:10.5px var(--mono);color:var(--dim);display:flex;gap:8px;align-items:center;
  border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:1}
.kbload{padding:10px 12px;color:var(--dim);font-size:11.5px}
.kbcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.kbcard{border:1px solid var(--line);border-radius:2px;background:var(--panel);padding:11px;cursor:pointer;min-width:0}
.kbcard:hover{background:var(--panel2);border-color:var(--jade-dim)}
.kbcard .t{font-size:12.5px;font-weight:600;color:var(--ink);margin-bottom:4px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.kbcard .d{font-size:11.5px;color:var(--ash);line-height:1.5;max-height:54px;overflow:hidden}
.kbcard .ty{font:9px var(--mono);color:var(--dim);letter-spacing:.12em;text-transform:uppercase;
  border:1px solid var(--line);padding:1px 5px;border-radius:2px;flex:none}
#kbnote{width:100%;min-height:60px;padding:7px 8px;resize:vertical;font-size:12px;line-height:1.5;max-width:880px}

/* ---------- palette ---------- */
#palwrap{position:fixed;inset:0;background:rgba(8,12,10,.62);display:none;
  z-index:50;padding-top:11vh;backdrop-filter:blur(2px)}
#palwrap.show{display:block}
#pal{width:min(680px,92vw);margin:0 auto;background:var(--panel);
  border:1px solid var(--line);border-radius:3px;box-shadow:0 18px 60px rgba(0,0,0,.5);
  overflow:hidden}
#palinput{width:100%;border:none;border-bottom:1px solid var(--line);background:var(--bg);
  padding:13px 16px;font:14px var(--ui);border-radius:0}
#palres{max-height:46vh;overflow-y:auto}
.palrow{display:flex;align-items:center;gap:11px;padding:8px 16px;cursor:pointer}
.palrow.on{background:var(--panel2);box-shadow:inset 2px 0 0 var(--jade)}
.palrow .nm{color:var(--ink);white-space:nowrap}
.palrow .pp{font:10.5px var(--mono);color:var(--dim);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;flex:1;direction:rtl;text-align:left}
.palrow .rt{font:9.5px var(--mono);color:var(--jade);border:1px solid var(--jade-dim);
  border-radius:2px;padding:1px 6px;flex:none;letter-spacing:.08em}
#palfoot{border-top:1px solid var(--line);padding:7px 16px;font:10.5px var(--mono);
  color:var(--dim);display:flex;gap:16px}
#palfoot kbd{border:1px solid var(--line);border-radius:2px;padding:0 4px;
  background:var(--bg)}
.palempty{padding:18px 16px;color:var(--dim);font-size:12.5px}

@media (max-width:1100px){#detail{position:fixed;right:0;top:52px;bottom:30px;
  background:var(--bg);z-index:10;box-shadow:-12px 0 40px rgba(0,0,0,.4)}}
@media (max-width:880px){
  body{grid-template-columns:0 minmax(0,1fr) auto;grid-template-areas:"top top top" "main main detail" "foot foot foot"}
  #rail{display:none}
  .lhead,.lrow{grid-template-columns:minmax(0,1fr) 86px 120px}
  .lhead .htg,.lhead .hac,.lrow .tg,.lrow .acts{display:none}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>

<header id="top">
  <div id="brand">
    <div class="glyph">CV</div>
    <div>
      <span class="name">CENAS VAULT</span>
      <span class="sub">LOCAL OPERATIONS FILE PORTAL</span>
    </div>
  </div>
  <div id="searchbox">
    <span class="lens">&#8981;</span>
    <input id="topsearch" type="text" placeholder="Search every file by name&hellip;" readonly>
    <kbd>Ctrl K</kbd>
  </div>
  <div id="topright">
    <div id="idxpill" title="Search index status"><span class="dot"></span><span id="idxtext">&hellip;</span></div>
    <button class="tbtn" id="syncbtn" title="Push vault + app database to AiCk now">&#8645; Sync AICK</button>
    <button class="tbtn" id="reindexbtn" title="Rebuild the search index">&#8635; Reindex</button>
  </div>
</header>

<nav id="rail">
  <div class="rsec">
    <div class="eyebrow"><span>App Database</span></div>
    <div id="kblist"></div>
  </div>
  <div class="rsec" id="sec-pins">
    <div class="eyebrow"><span>Pinned</span></div>
    <div id="pinlist"></div>
  </div>
  <div class="rsec">
    <div class="eyebrow"><span>Views</span></div>
    <button class="ritem" id="v-recent"><span class="ic">&#9716;</span><span class="lab">Recent files</span></button>
    <button class="ritem" id="v-pinned"><span class="ic">&#9733;</span><span class="lab">All pinned</span></button>
  </div>
  <div class="rsec">
    <div class="eyebrow"><span>Roots</span></div>
    <div id="rootlist"></div>
  </div>
  <div class="rsec" id="sec-tags">
    <div class="eyebrow"><span>Tags</span></div>
    <div id="taglist"></div>
  </div>
  <div class="rsec">
    <div class="eyebrow"><span>Links</span><button id="linkadd" title="Add link">+</button></div>
    <div id="linklist"></div>
    <div id="linkform">
      <input id="lf-label" placeholder="Label">
      <input id="lf-url" placeholder="https://&hellip;">
      <div class="row">
        <button class="minibtn" id="lf-save">Add link</button>
        <button class="minibtn" id="lf-cancel">Cancel</button>
      </div>
    </div>
  </div>
</nav>

<section id="main">
  <div id="pathline"></div>
  <div id="metarow"></div>
  <div id="listing"></div>
</section>

<aside id="detail">
  <button class="close" id="dclose" title="Close panel">&#10005;</button>
  <div id="dname"></div>
  <div id="dpath" title="Click to copy full path"></div>
  <div class="dgrid" id="dgrid"></div>
  <div id="dactions"></div>
  <div class="dsec">
    <div class="eyebrow">Tags</div>
    <input id="dtags" placeholder="comma, separated, tags">
    <div class="dhint">Enter saves &middot; tags show in the rail and boost search</div>
  </div>
  <div class="dsec">
    <div class="eyebrow">Note <span class="flash" id="noteflash">saved</span></div>
    <textarea id="dnote" placeholder="What this is, why it matters, where it&rsquo;s used&hellip;"></textarea>
    <div class="dhint">Saves when you click away</div>
  </div>
  <div class="dsec">
    <div class="eyebrow">Preview</div>
    <div id="dpreview"></div>
  </div>
</aside>

<footer id="foot">
  <span><span class="dot"></span><span id="footidx">starting&hellip;</span></span>
  <span id="footroots"></span>
  <span id="footsync"></span>
  <span class="right" id="footaddr"></span>
</footer>

<div id="palwrap">
  <div id="pal">
    <input id="palinput" type="text" placeholder="Type to search across all roots&hellip;" autocomplete="off" spellcheck="false">
    <div id="palres"></div>
    <div id="palfoot">
      <span><kbd>&#8593;&#8595;</kbd> move</span>
      <span><kbd>&#8629;</kbd> reveal in Vault</span>
      <span><kbd>shift &#8629;</kbd> open file</span>
      <span><kbd>esc</kbd> close</span>
    </div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const IMG_EXTS=new Set(["png","jpg","jpeg","gif","webp","svg","ico","bmp"]);

const S={roots:[],links:[],pins:[],tags:{},idx:{},sep:"/",
  mode:"dir",cwd:null,viewTag:null,entries:[],truncated:false,err:null,
  view:"list",sortKey:"name",sortDir:1,sel:null,
  palRes:[],palAll:[],palIdx:0,polling:false,
  kb:{categories:[]},kbTrail:[],kbFlat:[],machine:"CK",mirror:false};

const api=p=>fetch(p).then(r=>r.json());
const post=(p,b)=>fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(b||{})}).then(r=>r.json());

const lc=s=>s.toLowerCase();
function extOf(n){const i=n.lastIndexOf(".");return i>0?n.slice(i+1).toLowerCase():"";}
function badgeFor(e){
  if(e.dir)return '<span class="badge dir">DIR</span>';
  const x=extOf(e.name)||"&mdash;";
  return '<span class="badge">'+esc(x.slice(0,4).toUpperCase())+'</span>';
}
function fmtSize(e){
  if(e.dir)return "&mdash;";
  const b=e.size;
  if(b<1024)return b+" B";
  if(b<1048576)return (b/1024).toFixed(b<102400?1:0)+" KB";
  if(b<1073741824)return (b/1048576).toFixed(1)+" MB";
  return (b/1073741824).toFixed(2)+" GB";
}
function fmtAgo(t){
  if(!t)return "&mdash;";
  const s=Date.now()/1000-t;
  if(s<70)return "just now";
  if(s<3600)return Math.floor(s/60)+"m ago";
  if(s<86400)return Math.floor(s/3600)+"h ago";
  if(s<1209600)return Math.floor(s/86400)+"d ago";
  return new Date(t*1000).toLocaleDateString();
}
function rootFor(path){
  const pl=lc(path);
  for(const r of S.roots){const rl=lc(r.path);
    if(pl===rl||pl.startsWith(rl+S.sep))return r;}
  return null;
}
function parentOf(p){
  const i=p.lastIndexOf(S.sep);
  return i>0?p.slice(0,i):p;
}

/* ---------- state / rail ---------- */
async function loadState(focusFirst){
  const st=await api("/api/state");
  S.roots=st.roots;S.links=st.links;S.pins=st.pins;S.tags=st.tagcloud;
  S.idx=st.index;S.sep=st.sep;S.machine=st.machine||"CK";S.mirror=!!st.mirror;
  const kr=await api("/api/kb");
  S.kb=(kr.kb&&kr.kb.categories)?kr.kb:{categories:[]};
  S.kbFlat=[];
  (function fl(nodes,trail,lbl){for(const n of nodes||[]){
    const t=trail.concat([n.id]),L=lbl?lbl+" / "+n.label:n.label;
    S.kbFlat.push({label:n.label,full:L,trail:t});fl(n.children,t,L);}})(S.kb.categories,[],"");
  renderRail();renderStatus();renderSync(st.sync);
  $("#footaddr").textContent=st.host+":"+st.port+" \\u00b7 "+S.machine;
  $("#footroots").textContent=st.roots.length+" root"+(st.roots.length===1?"":"s");
  if(S.mirror){const b=$("#syncbtn");if(b)b.style.display="none";}
  if(S.idx.building)pollIndex();
  if(focusFirst){
    if(S.kb.categories.length)kbShowTrail([S.kb.categories[0].id]);
    else if(S.roots.length)openDir(S.roots[0].path);
  }
}
function renderSync(sync){
  const el=$("#footsync");if(!el)return;
  if(S.mirror){el.textContent="MIRROR of CK \\u00b7 read-only";return;}
  if(!sync||!sync.last){el.textContent="AICK sync: pending";return;}
  const ago=sync.last?fmtAgo(sync.last):"";
  el.textContent="AICK sync: "+(sync.ok?"ok":"FAIL")+(ago&&ago.indexOf("&")<0?" \\u00b7 "+ago:"");
}
function renderStatus(){
  const b=S.idx.building;
  $("#idxpill").classList.toggle("busy",!!b);
  $("#foot").classList.toggle("busy",!!b);
  const n=(S.idx.count||0).toLocaleString();
  $("#idxtext").textContent=b?("indexing "+n):(n+" indexed");
  $("#footidx").textContent=b?("indexing\u2026 "+n+" entries so far")
    :(n+" entries indexed"+(S.idx.truncated?" (capped)":"")
      +(S.idx.finished?" \u00b7 "+fmtAgo(S.idx.finished).replace("&mdash;",""):""));
}
function pollIndex(){
  if(S.polling)return;S.polling=true;
  const t=setInterval(async()=>{
    const st=await api("/api/state");S.idx=st.index;renderStatus();
    if(!st.index.building){clearInterval(t);S.polling=false;
      S.pins=st.pins;S.tags=st.tagcloud;renderRail();
      if(S.mode==="recent")showRecent();}
  },1200);
}
function renderRail(){
  $("#kblist").innerHTML=(S.kb.categories||[]).length?(S.kb.categories).map(c=>
    '<button class="ritem kbcat" data-id="'+esc(c.id)+'" title="'+esc(c.desc||"")+'">'
    +'<span class="ic">&#9670;</span><span class="lab">'+esc(c.label)+'</span></button>').join("")
    :'<div class="railnote">kb.json not loaded.</div>';
  document.querySelectorAll("#kblist .kbcat").forEach(b=>b.onclick=()=>kbShowTrail([b.dataset.id]));
  const pl=$("#pinlist");
  pl.innerHTML=S.pins.length?S.pins.map((p,i)=>
    '<button class="ritem pin" data-i="'+i+'" title="'+esc(p.path)+'">'
    +'<span class="ic star">&#9733;</span><span class="lab">'+esc(p.name)+'</span></button>'
  ).join(""):'<div class="railnote">Star anything to keep it here.</div>';
  pl.querySelectorAll(".pin").forEach(b=>b.onclick=()=>{
    const p=S.pins[+b.dataset.i];
    if(p.missing)return;
    p.dir?openDir(p.path):locate(p.path);
  });
  $("#rootlist").innerHTML=S.roots.map((r,i)=>
    '<button class="ritem root" data-i="'+i+'" title="'+esc(r.path)+'">'
    +'<span class="ic">&#9656;</span><span class="lab">'+esc(r.label)+'</span></button>'
  ).join("");
  document.querySelectorAll("#rootlist .root").forEach(b=>
    b.onclick=()=>openDir(S.roots[+b.dataset.i].path));
  const tags=Object.entries(S.tags).sort((a,b)=>b[1]-a[1]);
  $("#taglist").innerHTML=tags.length?tags.map(([t,c])=>
    '<button class="tagchip" data-t="'+esc(t)+'">'+esc(t)
    +' <span class="ct">'+c+'</span></button>').join("")
    :'<div class="railnote">Tag files to group them across folders.</div>';
  document.querySelectorAll(".tagchip").forEach(b=>b.onclick=()=>showTag(b.dataset.t));
  $("#linklist").innerHTML=S.links.map((l,i)=>
    '<div class="linkrow"><a class="ritem" href="'+esc(l.url)+'" target="_blank" rel="noopener">'
    +'<span class="ic">&#8599;</span><span class="lab">'+esc(l.label)+'</span></a>'
    +'<button class="rm" data-i="'+i+'" title="Remove link">&#10005;</button></div>'
  ).join("");
  document.querySelectorAll("#linklist .rm").forEach(b=>b.onclick=async()=>{
    await post("/api/links",{action:"remove",index:+b.dataset.i});loadState();
  });
  markActiveRail();
}
function markActiveRail(){
  document.querySelectorAll("#rail .ritem").forEach(b=>b.classList.remove("active"));
  if(S.mode==="kb"&&S.kbTrail.length)
    document.querySelectorAll("#kblist .kbcat").forEach(b=>{
      if(b.dataset.id===S.kbTrail[0])b.classList.add("active");});
  if(S.mode==="recent")$("#v-recent").classList.add("active");
  if(S.mode==="pinned")$("#v-pinned").classList.add("active");
  if(S.mode==="dir"&&S.cwd){
    const r=rootFor(S.cwd);
    if(r)document.querySelectorAll("#rootlist .root").forEach(b=>{
      if(S.roots[+b.dataset.i].path===r.path)b.classList.add("active");});
  }
}

/* ---------- views ---------- */
async function openDir(path){
  const res=await api("/api/list?path="+encodeURIComponent(path));
  S.mode="dir";S.cwd=path;S.viewTag=null;S.sel=null;
  S.entries=res.entries||[];S.truncated=!!res.truncated;S.err=res.error||null;
  hideDetail();render();markActiveRail();
}
async function showRecent(){
  const res=await api("/api/recent");
  S.mode="recent";S.cwd=null;S.sel=null;S.entries=res.entries;S.err=null;S.truncated=false;
  hideDetail();render();markActiveRail();
}
function showPinned(){
  S.mode="pinned";S.cwd=null;S.sel=null;S.err=null;S.truncated=false;
  S.entries=S.pins.slice();
  hideDetail();render();markActiveRail();
}
async function showTag(tag){
  const res=await api("/api/tagged?tag="+encodeURIComponent(tag));
  S.mode="tag";S.viewTag=tag;S.cwd=null;S.sel=null;S.entries=res.entries;
  S.err=null;S.truncated=false;
  hideDetail();render();markActiveRail();
}
async function locate(path){
  await openDir(parentOf(path));
  const e=S.entries.find(x=>lc(x.path)===lc(path));
  if(e)select(e,true);
}

/* ---------- render ---------- */
function sortedEntries(){
  const k=S.sortKey,d=S.sortDir;
  return S.entries.slice().sort((a,b)=>{
    if(a.dir!==b.dir)return a.dir?-1:1;
    let v=0;
    if(k==="name")v=lc(a.name)<lc(b.name)?-1:1;
    else if(k==="size")v=a.size-b.size;
    else if(k==="mtime")v=a.mtime-b.mtime;
    else if(k==="type")v=(a.dir?"":extOf(a.name))<(b.dir?"":extOf(b.name))?-1:1;
    return v*d||((lc(a.name)<lc(b.name))?-1:1);
  });
}
function render(){
  if(S.mode==="kb"){renderKBPathline();renderKBMeta();renderKBBody();return;}
  renderPathline();renderMeta();
  S.view==="grid"?renderGrid():renderList();
}
function renderKeepScroll(){
  const L=$("#listing"),st=L.scrollTop;render();L.scrollTop=st;
}
function renderPathline(){
  const el=$("#pathline");
  if(S.mode==="recent"){el.innerHTML='<span class="view">RECENT FILES</span>';return;}
  if(S.mode==="pinned"){el.innerHTML='<span class="view">PINNED</span>';return;}
  if(S.mode==="tag"){el.innerHTML='<span class="view">TAG</span><span class="sep">&#9656;</span><span class="viewq">'+esc(S.viewTag)+'</span>';return;}
  const r=rootFor(S.cwd);
  if(!r){el.innerHTML='<span class="view">'+esc(S.cwd||"")+'</span>';return;}
  let html='<span class="seg rootseg" data-p="'+esc(r.path)+'">'+esc(r.label.toUpperCase())+'</span>';
  const rest=S.cwd.slice(r.path.length).split(S.sep).filter(Boolean);
  let acc=r.path;
  for(const part of rest){
    acc+=S.sep+part;
    html+='<span class="sep">&#9656;</span><span class="seg" data-p="'+esc(acc)+'">'+esc(part)+'</span>';
  }
  el.innerHTML=html;
  el.querySelectorAll(".seg").forEach(s=>s.onclick=()=>openDir(s.dataset.p));
  el.scrollLeft=el.scrollWidth;
}
function renderMeta(){
  const m=$("#metarow");
  const n=S.entries.length;
  let ctx="";
  if(S.mode==="dir")ctx=n+" item"+(n===1?"":"s")+(S.truncated?" (showing first "+n+")":"");
  if(S.mode==="recent")ctx=n+" most recently modified files across all roots";
  if(S.mode==="pinned")ctx=n+" pinned";
  if(S.mode==="tag")ctx=n+" tagged";
  let html="";
  const r=S.mode==="dir"?rootFor(S.cwd):null;
  if(r&&lc(S.cwd)!==lc(r.path))
    html+='<button class="up" id="upbtn">&#8593; Up</button>';
  html+='<span>'+ctx+'</span><span class="spacer"></span>';
  html+='<div class="vtoggle">'
    +'<button id="vw-list" class="'+(S.view==="list"?"on":"")+'">List</button>'
    +'<button id="vw-grid" class="'+(S.view==="grid"?"on":"")+'">Grid</button></div>';
  m.innerHTML=html;
  const up=$("#upbtn");if(up)up.onclick=()=>openDir(parentOf(S.cwd));
  $("#vw-list").onclick=()=>{S.view="list";render();};
  $("#vw-grid").onclick=()=>{S.view="grid";render();};
}
function headBtn(key,label,cls){
  const on=S.sortKey===key;
  return '<button class="'+(cls||"")+(on?" on":"")+'" data-k="'+key+'">'+label
    +(on?(S.sortDir>0?" &#9650;":" &#9660;"):"")+'</button>';
}
function renderList(){
  const L=$("#listing");L.className="";
  if(S.err){L.innerHTML='<div class="emptymsg"><div class="big">Can&rsquo;t open this folder</div>'+esc(S.err)+'</div>';return;}
  if(!S.entries.length){L.innerHTML=emptyHtml();return;}
  let html='<div class="lhead">'+headBtn("name","Name")
    +'<span class="htg">'+headBtn("type","Tags / Type","")+'</span>'
    +headBtn("size","Size")+headBtn("mtime","Modified")
    +'<span class="hac"></span></div>';
  const rows=sortedEntries();
  html+=rows.map((e,i)=>{
    const tg=e.tags&&e.tags.length?"<b>"+e.tags.map(esc).join("</b> <b>")+"</b>":"";
    return '<div class="lrow'+(S.sel&&S.sel.path===e.path?" sel":"")+(e.missing?" miss":"")+'" data-i="'+i+'">'
      +'<div class="nm"><button class="pinstar'+(e.pinned?" on":"")+'" data-act="pin" title="Pin">'+(e.pinned?"&#9733;":"&#9734;")+'</button>'
      +badgeFor(e)+'<span class="t">'+esc(e.name)+'</span></div>'
      +'<div class="tg">'+tg+'</div>'
      +'<div class="sz">'+fmtSize(e)+'</div>'
      +'<div class="mt">'+fmtAgo(e.mtime)+'</div>'
      +'<div class="acts">'
      +(e.dir?'<button data-act="enter">Enter</button>':'<button data-act="open">Open</button>')
      +'<button data-act="reveal">Show</button>'
      +'<button data-act="copy">Copy</button></div></div>';
  }).join("");
  L.innerHTML=html;
  L.querySelectorAll(".lhead button[data-k]").forEach(b=>b.onclick=()=>{
    const k=b.dataset.k;
    if(S.sortKey===k)S.sortDir*=-1;else{S.sortKey=k;S.sortDir=1;}
    render();
  });
  wireRows(L,".lrow",sortedEntries());
}
function renderGrid(){
  const L=$("#listing");L.className="grid";
  if(S.err){L.className="";L.innerHTML='<div class="emptymsg"><div class="big">Can&rsquo;t open this folder</div>'+esc(S.err)+'</div>';return;}
  if(!S.entries.length){L.className="";L.innerHTML=emptyHtml();return;}
  const rows=sortedEntries();
  L.innerHTML=rows.map((e,i)=>{
    const isImg=!e.dir&&IMG_EXTS.has(extOf(e.name));
    const thumb=isImg
      ?'<img loading="lazy" src="/api/raw?path='+encodeURIComponent(e.path)+'" alt="">'
      :badgeFor(e);
    return '<div class="card'+(S.sel&&S.sel.path===e.path?" sel":"")+'" data-i="'+i+'">'
      +'<div class="thumb">'+thumb+'</div>'
      +'<div class="t" title="'+esc(e.name)+'">'+esc(e.name)+'</div>'
      +'<div class="s">'+(e.dir?"folder":fmtSize(e))+'</div></div>';
  }).join("");
  wireRows(L,".card",rows);
}
function emptyHtml(){
  if(S.mode==="pinned")return '<div class="emptymsg"><div class="big">Nothing pinned yet</div>Hit the &#9734; on any file or folder and it lands here.</div>';
  if(S.mode==="tag")return '<div class="emptymsg"><div class="big">No files carry this tag</div></div>';
  if(S.mode==="recent")return '<div class="emptymsg"><div class="big">Index is still building</div>Recent files appear once the first index pass finishes.</div>';
  return '<div class="emptymsg"><div class="big">Empty folder</div></div>';
}
function wireRows(container,selr,rows){
  container.querySelectorAll(selr).forEach(el=>{
    const e=rows[+el.dataset.i];
    el.addEventListener("click",ev=>{
      const act=ev.target.dataset&&ev.target.dataset.act;
      if(act){ev.stopPropagation();rowAction(act,e);return;}
      select(e,false);
    });
    el.addEventListener("dblclick",()=>{e.dir?openDir(e.path):post("/api/open",{path:e.path});});
  });
}
async function rowAction(act,e){
  if(act==="enter")return openDir(e.path);
  if(act==="open")return post("/api/open",{path:e.path});
  if(act==="reveal")return post("/api/reveal",{path:e.path});
  if(act==="copy")return navigator.clipboard.writeText(e.path);
  if(act==="pin"){
    if(S.mirror)return;
    await post("/api/meta",{path:e.path,pin:!e.pinned});
    e.pinned=!e.pinned;renderKeepScroll();refreshRail();
    if(S.sel&&S.sel.path===e.path)showDetail(e);
  }
}
async function refreshRail(){
  const st=await api("/api/state");
  S.pins=st.pins;S.tags=st.tagcloud;S.links=st.links;renderRail();
}

/* ---------- knowledge base (app database) ---------- */
function kbFind(trail){
  let nodes=S.kb.categories,out=[];
  for(const id of trail){
    const n=(nodes||[]).find(x=>x.id===id);
    if(!n)return null;
    out.push(n);nodes=n.children||[];
  }
  return out.length?out:null;
}
function kbShowTrail(trail){
  const chain=kbFind(trail);
  if(!chain)return;
  S.mode="kb";S.kbTrail=trail.slice();S.cwd=null;S.viewTag=null;S.sel=null;
  hideDetail();render();markActiveRail();
  $("#listing").scrollTop=0;
}
function statusBadge(st){
  if(!st||st==="current")return "";
  const cls=st==="stale"?"stale":st==="hold"?"hold":st==="soon"?"soon":"unknown";
  return '<span class="kbb '+cls+'">'+esc(String(st).toUpperCase())+'</span>';
}
function renderKBPathline(){
  const chain=kbFind(S.kbTrail)||[];
  let html='<span class="seg rootseg" data-t="">APP DB</span>';
  const acc=[];
  for(const n of chain){
    acc.push(n.id);
    html+='<span class="sep">&#9656;</span><span class="seg" data-t="'+esc(acc.join("|"))+'">'+esc(n.label)+'</span>';
  }
  const el=$("#pathline");el.innerHTML=html;
  el.querySelectorAll(".seg").forEach(s=>s.onclick=()=>{
    const t=s.dataset.t;
    if(t)kbShowTrail(t.split("|"));
    else if(S.kb.categories.length)kbShowTrail([S.kb.categories[0].id]);
  });
  el.scrollLeft=el.scrollWidth;
}
function renderKBMeta(){
  const chain=kbFind(S.kbTrail),n=chain[chain.length-1];
  const kids=(n.children||[]).length;
  let html='<span>'+esc(n.type||"entry")+(kids?' &middot; '+kids+' item'+(kids===1?"":"s"):'')+'</span>';
  html+='<span class="spacer"></span>';
  if(S.mirror)html+='<span class="kbb aick">MIRROR &middot; READ-ONLY</span>';
  $("#metarow").innerHTML=html;
}
function renderKBBody(){
  const chain=kbFind(S.kbTrail),n=chain[chain.length-1];
  const L=$("#listing");L.className="kbwrap";
  let h="<div>";
  const machines=[...new Set((n.paths||[]).map(p=>p.machine||"CK"))];
  let bd=machines.map(m=>'<span class="kbb '+(m==="AICK"?"aick":"ck")+'">'+(m==="AICK"?"ON AICK":"ON CK")+'</span>').join("");
  bd+=statusBadge(n.status);
  if(bd)h+='<div>'+bd+'</div>';
  if(n.desc)h+='<div class="kbdesc">'+esc(n.desc)+'</div>';
  if(n.how)h+='<div class="kbhow">'+esc(n.how)+'</div>';
  let g="";
  if(n.route)g+='<span class="k">Route</span><span class="v">'+esc(n.route)
    +(n.live?' &middot; <a href="'+esc(n.live)+'" target="_blank" rel="noopener">open live</a>':'')+'</span>';
  if(n.files&&n.files.length)g+='<span class="k">Source</span><span class="v">'+n.files.map(esc).join("<br>")+'</span>';
  if(n.data&&n.data.length)g+='<span class="k">Data</span><span class="v">'+n.data.map(esc).join(", ")+'</span>';
  if(g)h+='<div class="kbgrid">'+g+'</div>';
  if(n.links&&n.links.length)
    h+='<div class="kbsec"><div class="eyebrow">Links</div>'+n.links.map(l=>
      '<div class="kblink"><a href="'+esc(l.url)+'" target="_blank" rel="noopener">'+esc(l.label)+'</a>'
      +'<span class="u">'+esc(l.url)+'</span></div>').join("")+'</div>';
  if(n.paths&&n.paths.length)
    h+='<div class="kbsec"><div class="eyebrow">On disk</div>'+n.paths.map((p,i)=>{
      const m=p.machine||"CK",here=m===S.machine;
      return '<div class="kbpath"><div class="hd">'
        +'<span class="kbb '+(m==="AICK"?"aick":"ck")+'">'+esc(m)+'</span>'
        +(p.stale?'<span class="kbb stale">STALE</span>':'')
        +'<span class="pp">'+esc(p.path)
        +(p.label?' <span class="pl">&middot; '+esc(p.label)+'</span>':'')+'</span>'
        +(here?'<button class="minibtn" data-br="'+i+'">Contents</button>'
              +'<button class="minibtn" data-op="'+i+'">Open folder</button>'
             :'<span class="pl">on the other machine</span>')
        +'</div><div class="kbpane" id="kbp'+i+'" data-open="0"></div></div>';
    }).join("")+'</div>';
  if(n.children&&n.children.length)
    h+='<div class="kbsec"><div class="eyebrow">Inside '+esc(n.label)+'</div><div class="kbcards">'
      +n.children.map(c=>'<div class="kbcard" data-c="'+esc(c.id)+'">'
        +'<div class="t"><span>'+esc(c.label)+'</span>'
        +(c.type?'<span class="ty">'+esc(c.type)+'</span>':'')+statusBadge(c.status)+'</div>'
        +'<div class="d">'+esc(c.desc||"")+'</div></div>').join("")
      +'</div></div>';
  h+='<div class="kbsec"><div class="eyebrow">Note'+(S.mirror?' (read-only on mirror)':'')+'</div>'
    +'<textarea id="kbnote" '+(S.mirror?'readonly ':'')
    +'placeholder="Operator note for this entry&hellip;">'+esc(n.note||"")+'</textarea></div>';
  h+="</div>";
  L.innerHTML=h;
  L.querySelectorAll(".kbcard").forEach(c=>c.onclick=()=>kbShowTrail(S.kbTrail.concat([c.dataset.c])));
  L.querySelectorAll("[data-br]").forEach(b=>b.onclick=()=>kbBrowse(n.paths[+b.dataset.br].path,"kbp"+b.dataset.br));
  L.querySelectorAll("[data-op]").forEach(b=>b.onclick=()=>post("/api/open",{path:n.paths[+b.dataset.op].path}));
  const nt=$("#kbnote");
  if(nt&&!S.mirror)nt.onblur=async()=>{
    if(nt.value===(n.note||""))return;
    await post("/api/kb/note",{id:n.id,note:nt.value});
    n.note=nt.value;
  };
}
async function kbBrowse(path,paneId){
  const pane=document.getElementById(paneId);
  if(!pane)return;
  if(pane.dataset.open==="1"){pane.dataset.open="0";pane.innerHTML="";return;}
  pane.dataset.open="1";pane.dataset.base=path;
  kbPaneLoad(pane,path);
}
async function kbPaneLoad(pane,path){
  pane.innerHTML='<div class="kbload">&hellip;</div>';
  const r=await api("/api/list?path="+encodeURIComponent(path));
  if(r.error){pane.innerHTML='<div class="kbload">'+esc(r.error)+'</div>';return;}
  const base=pane.dataset.base,ents=r.entries||[];
  let h='<div class="kbcrumb"><span style="flex:1;word-break:break-all">'+esc(path)+'</span>'
    +(lc(path)!==lc(base)?'<button class="minibtn" data-up="1">&#8593; up</button>':'')
    +'<button class="minibtn" data-vault="1">open in Vault view</button></div>';
  h+=ents.map((e,i)=>'<div class="kbrow">'+badgeFor(e)
    +'<span class="t" data-i="'+i+'" title="'+esc(e.path)+'">'+esc(e.name)+'</span>'
    +'<span class="z">'+(e.dir?"":fmtSize(e))+'</span>'
    +'<span class="z">'+fmtAgo(e.mtime)+'</span></div>').join("");
  if(!ents.length)h+='<div class="kbload">empty folder</div>';
  if(r.truncated)h+='<div class="kbload">list capped at first '+ents.length+'</div>';
  pane.innerHTML=h;
  const up=pane.querySelector("[data-up]");
  if(up)up.onclick=()=>kbPaneLoad(pane,parentOf(path));
  pane.querySelector("[data-vault]").onclick=()=>openDir(path);
  pane.querySelectorAll(".kbrow .t").forEach(t=>{
    const e=ents[+t.dataset.i];
    t.onclick=()=>{e.dir?kbPaneLoad(pane,e.path):post("/api/open",{path:e.path});};
  });
}

/* ---------- detail panel ---------- */
function select(e,scrollTo){
  S.sel=e;renderKeepScroll();showDetail(e);
  if(scrollTo){const r=document.querySelector(".lrow.sel,.card.sel");
    if(r)r.scrollIntoView({block:"center"});}
}
function hideDetail(){$("#detail").classList.remove("show");S.sel=null;}
function showDetail(e){
  const D=$("#detail");D.classList.add("show");
  $("#dname").textContent=e.name;
  $("#dpath").textContent=e.path;
  $("#dgrid").innerHTML=
    '<span class="k">Kind</span><span class="v">'+(e.dir?"Folder":esc(extOf(e.name).toUpperCase()||"File"))+'</span>'
    +'<span class="k">Size</span><span class="v">'+fmtSize(e)+'</span>'
    +'<span class="k">Modified</span><span class="v">'+(e.mtime?new Date(e.mtime*1000).toLocaleString():"&mdash;")+'</span>';
  $("#dactions").innerHTML=
    (e.dir?'<button class="minibtn primary" id="da-enter">Open folder</button>'
          :'<button class="minibtn primary" id="da-open">Open file</button>')
    +'<button class="minibtn" id="da-reveal">Show in Explorer</button>'
    +'<button class="minibtn" id="da-copy">Copy path</button>';
  const en=$("#da-enter");if(en)en.onclick=()=>openDir(e.path);
  const op=$("#da-open");if(op)op.onclick=()=>post("/api/open",{path:e.path});
  $("#da-reveal").onclick=()=>post("/api/reveal",{path:e.path});
  $("#da-copy").onclick=()=>navigator.clipboard.writeText(e.path);
  $("#dpath").onclick=()=>navigator.clipboard.writeText(e.path);
  const tagsEl=$("#dtags");tagsEl.value=(e.tags||[]).join(", ");
  tagsEl.disabled=S.mirror;
  tagsEl.onkeydown=async ev=>{
    if(ev.key!=="Enter")return;
    const tags=tagsEl.value.split(",").map(t=>t.trim()).filter(Boolean);
    await post("/api/meta",{path:e.path,tags});
    e.tags=tags;renderKeepScroll();refreshRail();
  };
  const noteEl=$("#dnote");noteEl.value=e.note||"";
  noteEl.disabled=S.mirror;
  noteEl.onblur=async()=>{
    if(noteEl.value===(e.note||""))return;
    await post("/api/meta",{path:e.path,note:noteEl.value});
    e.note=noteEl.value;
    const f=$("#noteflash");f.classList.add("on");setTimeout(()=>f.classList.remove("on"),900);
  };
  loadPreview(e);
}
async function loadPreview(e){
  const P=$("#dpreview");
  if(e.dir){P.innerHTML='<div class="none">Folder &mdash; open it to browse contents.</div>';return;}
  if(IMG_EXTS.has(extOf(e.name))){
    P.innerHTML='<img src="/api/raw?path='+encodeURIComponent(e.path)+'" alt="">';return;
  }
  P.innerHTML='<div class="none">&hellip;</div>';
  const r=await api("/api/preview?path="+encodeURIComponent(e.path));
  if(r.kind==="text")
    P.innerHTML='<pre>'+esc(r.content)+(r.truncated?"\n\u2026":"")+'</pre>';
  else P.innerHTML='<div class="none">No preview for this file type.</div>';
}
$("#dclose").onclick=hideDetail;

/* ---------- palette ---------- */
const palwrap=$("#palwrap"),palinput=$("#palinput"),palres=$("#palres");
let palTimer=null;
function openPal(){palwrap.classList.add("show");palinput.value="";
  S.palAll=[];S.palIdx=0;renderPal();palinput.focus();}
function closePal(){palwrap.classList.remove("show");}
palinput.addEventListener("input",()=>{
  clearTimeout(palTimer);
  palTimer=setTimeout(async()=>{
    const q=palinput.value.trim();
    if(!q){S.palAll=[];renderPal();return;}
    const kbHits=S.kbFlat.filter(k=>lc(k.full).includes(lc(q))).slice(0,4)
      .map(k=>({kbhit:true,name:k.label,full:k.full,trail:k.trail}));
    const r=await api("/api/search?q="+encodeURIComponent(q));
    S.palAll=kbHits.concat(r.results||[]);S.palIdx=0;renderPal();
  },140);
});
function renderPal(){
  if(!palinput.value.trim()){
    palres.innerHTML='<div class="palempty">'+(S.idx.building
      ?"Index is still building \u2014 results may be partial."
      :"Search by file name; matches in the full path count too.")+'</div>';
    return;
  }
  if(!S.palAll.length){palres.innerHTML='<div class="palempty">No matches for &ldquo;'+esc(palinput.value)+'&rdquo;.</div>';return;}
  palres.innerHTML=S.palAll.map((e,i)=>{
    if(e.kbhit)return '<div class="palrow'+(i===S.palIdx?" on":"")+'" data-i="'+i+'">'
      +'<span class="badge dir">KB</span><span class="nm">'+esc(e.name)+'</span>'
      +'<span class="pp">&#x200e;'+esc(e.full)+'</span>'
      +'<span class="rt">APP DB</span></div>';
    return '<div class="palrow'+(i===S.palIdx?" on":"")+'" data-i="'+i+'">'
      +badgeFor(e)+'<span class="nm">'+esc(e.name)+'</span>'
      +'<span class="pp">&#x200e;'+esc(e.path)+'</span>'
      +'<span class="rt">'+esc(e.root||"")+'</span></div>';
  }).join("");
  palres.querySelectorAll(".palrow").forEach(r=>{
    r.onclick=()=>palGo(+r.dataset.i,false);
  });
  const on=palres.querySelector(".palrow.on");
  if(on)on.scrollIntoView({block:"nearest"});
}
function palGo(i,osOpen){
  const e=S.palAll[i];if(!e)return;
  closePal();
  if(e.kbhit)return kbShowTrail(e.trail);
  if(osOpen&&!e.dir)return post("/api/open",{path:e.path});
  e.dir?openDir(e.path):locate(e.path);
}
document.addEventListener("keydown",ev=>{
  const inPal=palwrap.classList.contains("show");
  if((ev.ctrlKey||ev.metaKey)&&ev.key.toLowerCase()==="k"){ev.preventDefault();inPal?closePal():openPal();return;}
  if(!inPal&&ev.key==="/"&&!/INPUT|TEXTAREA/.test(document.activeElement.tagName)){ev.preventDefault();openPal();return;}
  if(!inPal)return;
  if(ev.key==="Escape"){closePal();}
  else if(ev.key==="ArrowDown"){ev.preventDefault();S.palIdx=Math.min(S.palIdx+1,S.palAll.length-1);renderPal();}
  else if(ev.key==="ArrowUp"){ev.preventDefault();S.palIdx=Math.max(S.palIdx-1,0);renderPal();}
  else if(ev.key==="Enter"){ev.preventDefault();palGo(S.palIdx,ev.shiftKey);}
});
$("#topsearch").addEventListener("focus",e=>{e.target.blur();openPal();});
$("#topsearch").addEventListener("click",openPal);

/* ---------- misc wiring ---------- */
$("#v-recent").onclick=showRecent;
$("#v-pinned").onclick=showPinned;
$("#reindexbtn").onclick=async()=>{await post("/api/reindex");S.idx.building=true;renderStatus();pollIndex();};
$("#syncbtn").onclick=async()=>{
  const b=$("#syncbtn");b.textContent="syncing\\u2026";
  const r=await post("/api/sync");
  b.innerHTML="&#8645; Sync AICK";
  renderSync(r);
};
$("#linkadd").onclick=()=>$("#linkform").classList.toggle("show");
$("#lf-cancel").onclick=()=>$("#linkform").classList.remove("show");
$("#lf-save").onclick=async()=>{
  const label=$("#lf-label").value.trim(),url=$("#lf-url").value.trim();
  if(!label||!url)return;
  await post("/api/links",{action:"add",label,url});
  $("#lf-label").value="";$("#lf-url").value="";
  $("#linkform").classList.remove("show");loadState();
};

loadState(true);
</script>
</body>
</html>"""

# ===========================================================================
#  HTTP layer
# ===========================================================================
class VaultHandler(BaseHTTPRequestHandler):
    server_version = "CenasVaultCloud/1.0"

    def log_message(self, *args):
        pass

    # -- auth -----------------------------------------------------------------
    # Every route, including "/" and unknown paths, requires HTTP Basic auth
    # (user "sam", password VAULT_TOKEN). Both halves are compared with
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
        body = b'{"ok": false}'
        try:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="cenas-vault"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass
        self.close_connection = True
        return False

    # -- helpers ------------------------------------------------------------
    def _send(self, code, ctype, body, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj).encode("utf-8"))

    def _bad(self, msg, code=400):
        self._json({"error": msg}, code)

    def _qs(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _safe_from_qs(self):
        q = self._qs()
        p = (q.get("path") or [""])[0]
        return safe_path(p)

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
            self._send(200, "text/html; charset=utf-8", HTML.encode("utf-8"))
        elif route == "/api/state":
            self._json(self._state())
        elif route == "/api/kb":
            self._json({"kb": _kb_fresh(), "machine": MACHINE,
                        "mirror": MIRROR, "sync": dict(SYNC_STATUS)})
        elif route == "/api/list":
            p = self._safe_from_qs()
            if not p:
                return self._bad("path outside configured roots")
            res, err = list_dir(p)
            self._json({"error": err, "entries": [], "truncated": False} if err else res)
        elif route == "/api/search":
            q = (self._qs().get("q") or [""])[0]
            self._json({"results": search_index(q)})
        elif route == "/api/recent":
            self._json({"entries": recent_files()})
        elif route == "/api/tagged":
            t = (self._qs().get("tag") or [""])[0]
            self._json({"entries": tagged_entries(t)})
        elif route == "/api/preview":
            self._preview()
        elif route == "/api/raw":
            self._raw()
        elif route == "/sync/manifest":
            self._sync_manifest()
        elif route == "/sync/file":
            self._sync_file_get()
        else:
            self._bad("not found", 404)

    def _state(self):
        _meta_fresh()
        pins = []
        with _META_LOCK:
            pin_paths = list(META["pins"].values())
            tagcloud = {}
            for tags in META["tags"].values():
                for t in tags:
                    tagcloud[t] = tagcloud.get(t, 0) + 1
            links = list(META["links"])
        for p in sorted(pin_paths, key=lambda x: os.path.basename(x).lower()):
            pins.append(_stat_entry(p))
        return {
            "roots": ROOTS_RESOLVED,
            "links": links,
            "pins": pins,
            "tagcloud": tagcloud,
            "index": dict(INDEX_STATUS),
            "sep": os.sep,
            "host": HOST,
            "port": PORT,
            "platform": platform.system(),
            "machine": MACHINE,
            "mirror": MIRROR,
            "sync": dict(SYNC_STATUS),
        }

    def _preview(self):
        p = self._safe_from_qs()
        if not p or not os.path.isfile(p):
            return self._bad("not a file in configured roots")
        ext = os.path.splitext(p)[1].lower()
        name = os.path.basename(p).lower()
        if ext in IMG_EXTS:
            return self._json({"kind": "image"})
        if ext in TEXT_EXTS or name in TEXT_EXTS or ext == "":
            try:
                size = os.path.getsize(p)
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(PREVIEW_BYTES)
                return self._json({"kind": "text", "content": content,
                                   "truncated": size > PREVIEW_BYTES})
            except OSError as e:
                return self._json({"kind": "none", "error": str(e)})
        self._json({"kind": "none"})

    def _raw(self):
        p = self._safe_from_qs()
        if not p or not os.path.isfile(p):
            return self._bad("not a file in configured roots", 404)
        try:
            if os.path.getsize(p) > RAW_CAP:
                return self._bad("file too large to preview", 413)
            with open(p, "rb") as f:
                data = f.read()
        except OSError as e:
            return self._bad(str(e), 500)
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        self._send(200, ctype, data, cache=True)

    # -- POST ---------------------------------------------------------------
    def do_POST(self):
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
            return self._sync_file_post()      # raw body, not JSON
        if route == "/sync/tombstone":
            return self._sync_tombstone()
        if route == "/sync/kb":
            return self._sync_kb()             # raw body, not JSON
        body = self._body()
        if route in ("/api/open", "/api/reveal"):
            return self._json({"ok": False, "error": "local-only"}, 400)
        if route == "/api/meta":
            return self._meta(body)
        if route == "/api/links":
            return self._links(body)
        if route == "/api/kb/note":
            return self._kb_note(body)
        if route == "/api/sync":
            return self._json(sync_to_aick())
        if route == "/api/reindex":
            reindex_async()
            return self._json({"ok": True})
        self._bad("not found", 404)

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
            return self._json({"ok": False, "error": "unknown hash"}, 404)
        rel, dest = sync_dest(row[0])
        if not dest or not os.path.isfile(dest):
            return self._json({"ok": False, "error": "file missing on disk"}, 404)
        try:
            with open(dest, "rb") as f:
                data = f.read()
        except OSError:
            return self._json({"ok": False, "error": "read failed"}, 500)
        self._send(200, "application/octet-stream", data)

    def _sync_file_post(self):
        sha = (self.headers.get("X-Sha256") or "").strip().lower()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        data = self.rfile.read(n) if n > 0 else b""
        try:
            mtime = float(self.headers.get("X-Mtime") or "")
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "bad X-Mtime"}, 400)
        # Hash check BEFORE anything touches the disk.
        if not sha or hashlib.sha256(data).hexdigest() != sha:
            return self._json({"ok": False, "error": "sha256 mismatch"}, 400)
        rel, dest = sync_dest(self.headers.get("X-Relpath") or "")
        if not rel or blocked_relpath(rel):
            return self._json({"ok": False, "error": "relpath rejected"}, 400)
        if os.path.isdir(dest):
            return self._json({"ok": False, "error": "relpath is a directory"}, 400)
        quarantined = None
        if os.path.isfile(dest):
            try:
                old_sha = file_sha256(dest)
            except OSError:
                old_sha = None
            if old_sha != sha:
                quarantined = quarantine(dest, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
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
                (rel, len(data), sha, mtime))
            db_log(conn, "put", rel,
                   "ok" + (" quarantined-old" if quarantined else ""))
            conn.commit()
        finally:
            conn.close()
        self._json({"ok": True, "relpath": rel, "size": len(data),
                    "quarantined": bool(quarantined)})

    def _sync_kb(self):
        # Receive the KB tree (kb.json) from the local sync worker and write it
        # to KB_FILE (/var/data/kb.json), which the UI hot-reloads on mtime.
        # Auth is already enforced (Basic). Verify sha256 + that the body parses
        # as a categories-shaped JSON doc BEFORE anything touches the disk.
        sha = (self.headers.get("X-Sha256") or "").strip().lower()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        data = self.rfile.read(n) if n > 0 else b""
        if not sha or hashlib.sha256(data).hexdigest() != sha:
            return self._json({"ok": False, "error": "sha256 mismatch"}, 400)
        try:
            obj = json.loads(data.decode("utf-8"))
        except Exception:
            return self._json({"ok": False, "error": "not valid JSON"}, 400)
        if not (isinstance(obj, dict) and isinstance(obj.get("categories"), list)):
            return self._json({"ok": False, "error": "kb shape invalid"}, 400)
        try:
            os.makedirs(os.path.dirname(KB_FILE) or ".", exist_ok=True)
            tmp = KB_FILE + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, KB_FILE)
        except OSError as e:
            return self._json({"ok": False, "error": "write failed: %s" % e}, 500)
        return self._json({"ok": True, "bytes": len(data),
                           "categories": len(obj.get("categories") or [])})

    def _sync_tombstone(self):
        body = self._body()
        rel, dest = sync_dest(body.get("relpath", ""))
        if not rel or blocked_relpath(rel):
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

    def _kb_note(self, body):
        nid = str(body.get("id", ""))
        note = str(body.get("note", ""))
        with KB_LOCK:
            n = _kb_find(nid)
            if not n:
                return self._bad("unknown kb id")
            if note.strip():
                n["note"] = note
            else:
                n.pop("note", None)
        _save_kb()
        self._json({"ok": True})

    def _meta(self, body):
        p = safe_path(body.get("path", ""))
        if not p:
            return self._bad("path outside configured roots")
        k = _mkey(p)
        with _META_LOCK:
            if "pin" in body:
                if body["pin"]:
                    META["pins"][k] = p
                else:
                    META["pins"].pop(k, None)
            if "tags" in body:
                tags = [str(t).strip() for t in body["tags"] if str(t).strip()]
                if tags:
                    META["tags"][k] = tags
                else:
                    META["tags"].pop(k, None)
            if "note" in body:
                note = str(body["note"])
                if note.strip():
                    META["notes"][k] = note
                else:
                    META["notes"].pop(k, None)
            _save_meta()
        self._json({"ok": True, **meta_for(p)})

    def _links(self, body):
        action = body.get("action")
        with _META_LOCK:
            if action == "add":
                label = str(body.get("label", "")).strip()
                url = str(body.get("url", "")).strip()
                if not label or not url:
                    return self._bad("label and url required")
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                META["links"].append({"label": label, "url": url})
            elif action == "remove":
                i = body.get("index", -1)
                if isinstance(i, int) and 0 <= i < len(META["links"]):
                    META["links"].pop(i)
            else:
                return self._bad("unknown action")
            _save_meta()
        self._json({"ok": True, "links": META["links"]})


# ===========================================================================
#  Entry point
# ===========================================================================
def main():
    # Env-only config; no CLI args, no browser open, no ssh push in the cloud.
    db_init()
    reindex_async()
    server = ThreadingHTTPServer((HOST, PORT), VaultHandler)
    print(f"cenas-vault-cloud listening on 0.0.0.0:{PORT} root={VAULT_ROOT}",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Vault closed.")


if __name__ == "__main__":
    main()
