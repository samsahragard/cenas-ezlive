"""Phase 0 (Sam #2901) -- build the empty CK performance DB + print proof.
Idempotent: re-running only (re)applies CREATE TABLE IF NOT EXISTS + meta.
Source of truth lives ON CK / Mini_IT13. No prod DB is touched.
"""
import sqlite3, hashlib, os, datetime

DB_DIR = r"C:\Users\sam\cena-perfdb"
DB = os.path.join(DB_DIR, "perf.sqlite")
SCHEMA = os.path.join(DB_DIR, "schema_v1.sql")
SCHEMA_VERSION = "1"
DATA_TABLES = ["employee", "employee_store", "perf_period",
               "perf_internal", "time_entry", "sync_run"]


def build():
    ddl = open(SCHEMA, encoding="utf-8").read()
    schema_hash = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    con = sqlite3.connect(DB)
    try:
        con.executescript(ddl)
        now = datetime.datetime.now().isoformat(timespec="seconds")
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (SCHEMA_VERSION,))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_hash',?)", (schema_hash,))
        con.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('created_at',?)", (now,))
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_file',?)", ("schema_v1.sql",))
        con.commit()
    finally:
        con.close()
    return schema_hash


def proof():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    line = "=" * 60
    print(line)
    print("PHASE 0 PROOF -- CK performance DB (Sam #2901)")
    print(line)
    print("DB PATH      :", DB)
    print("FILE SIZE    :", os.path.getsize(DB), "bytes")
    tabs = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(".tables      :", " ".join(tabs))
    idxs = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print("indexes      :", " ".join(idxs) or "(none)")
    print("integrity_chk:", cur.execute("PRAGMA integrity_check").fetchone()[0])
    meta = dict(cur.execute("SELECT key,value FROM meta"))
    print("schema_ver   :", meta.get("schema_version"))
    print("schema_hash  :", meta.get("schema_hash"))
    print("created_at   :", meta.get("created_at"))
    print("-" * 60)
    print("ROW COUNTS (data tables must be 0 before load):")
    all_zero = True
    for t in DATA_TABLES:
        n = cur.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        if n:
            all_zero = False
        print("  %-16s %d" % (t, n))
    meta_n = cur.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    print("  %-16s %d  (config rows, expected)" % ("meta", meta_n))
    print("-" * 60)
    print("RESULT       :", "PASS -- empty schema ready" if all_zero else "FAIL -- data present")
    print(line)
    con.close()


if __name__ == "__main__":
    build()
    proof()
