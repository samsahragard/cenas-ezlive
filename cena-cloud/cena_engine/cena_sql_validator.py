"""C.E.N.A. Level 3 - SQL correctness/safety gate.

Owner: Subagent A. Contract: docs/cena_level3_contracts.md section 3.

    validate_sql(sql: str) -> tuple[bool, str]   # (ok, reason); reason "" when ok

Policy (frozen):
- sqlglot, sqlite dialect. Exactly one statement. SELECT only (WITH...SELECT and
  set operations of SELECTs allowed). Everything write/DDL/admin-shaped rejected.
- Every referenced table must be allowlisted (schema qualifiers + aliases
  resolved; CTE names defined in the query are legal sources).
- Explicit columns must be in the referenced table's allowlist. Unqualified
  columns in multi-table scopes: accepted only when exactly one referenced table
  carries the column, otherwise rejected as ambiguous-or-excluded naming the
  candidates.
- SELECT * / alias.* rejected against raw tables that carry exclusions (reason
  enumerates the allowed columns); allowed on analytics tables and raw tables
  without exclusions.
- Dangerous functions rejected. LIMIT is NOT required (executor injects it).
- Rejection reasons are the self-repair signal: specific + actionable.
- This module imports ONLY sqlglot + cena_sql_schema (never the executor or
  analytics modules).
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope

from cena_engine import cena_sql_schema

_DANGEROUS_FUNCS = frozenset(
    {"load_extension", "readfile", "writefile", "fts3_tokenizer", "zipfile", "edit"}
)

# Table-valued JSON functions are legal opaque sources (needed for *_json columns).
_TABLE_FUNCS = frozenset({"json_each", "json_tree"})

_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Create", "Drop", "Alter", "Merge",
    "TruncateTable", "Pragma", "Attach", "Detach", "Analyze", "Command",
)
_FORBIDDEN_NODES = tuple(
    cls for cls in (getattr(exp, n, None) for n in _FORBIDDEN_NODE_NAMES) if cls
)

_ALLOWED_ROOTS = tuple(
    cls
    for cls in (
        exp.Select,
        getattr(exp, "Union", None),
        getattr(exp, "Except", None),
        getattr(exp, "Intersect", None),
        getattr(exp, "SetOperation", None),
        getattr(exp, "Subquery", None),
    )
    if cls
)

_IMPLICIT_COLS = frozenset({"rowid", "oid", "_rowid_"})

_OPAQUE = None  # sentinel value in source maps: CTE/subquery/table-func source


def _leading_keyword(sql: str) -> str:
    """First keyword after stripping comments and leading parens."""
    s = sql
    while True:
        s = s.lstrip()
        if s.startswith("--"):
            nl = s.find("\n")
            s = "" if nl < 0 else s[nl + 1:]
            continue
        if s.startswith("/*"):
            end = s.find("*/")
            s = "" if end < 0 else s[end + 2:]
            continue
        break
    s = s.lstrip("(").lstrip()
    m = re.match(r"[A-Za-z_]+", s)
    return m.group(0).lower() if m else (s[:12] or "<empty>")


def _fmt_cols(cols) -> str:
    return ", ".join(sorted(cols))


def _schema_tables(allowlist: dict, alias: str) -> str:
    names = sorted(k.split(".", 1)[1] for k in allowlist if k.startswith(alias + "."))
    return ", ".join(names)


def _resolve_table(
    table: exp.Table, allowlist: dict, analytics: set
) -> tuple[bool, str | None, str]:
    """-> (ok, qualified_key_or_None_for_opaque, reason)."""
    db = (table.db or "").lower()
    name = (table.name or "").lower()
    if not name or isinstance(table.this, exp.Func):
        if name and name not in _TABLE_FUNCS and not isinstance(table.this, exp.Func):
            pass  # unreachable; defensive
        return True, _OPAQUE, ""
    if name in _TABLE_FUNCS:
        return True, _OPAQUE, ""
    if db in ("", "main"):
        if name in analytics:
            return True, name, ""
        matches = sorted(k for k in allowlist if k.endswith("." + name))
        if matches:
            return False, None, (
                f"table '{name}' must be schema-qualified; use "
                f"{' or '.join(repr(m) for m in matches)}. Analytics tables "
                f"(unqualified) are: {', '.join(sorted(analytics))}"
            )
        return False, None, (
            f"table '{name}' is not allowlisted. Unqualified names must be one of "
            f"the analytics tables: {', '.join(sorted(analytics))}; raw tables are "
            f"qualified as appdb./toast./toastdm./ordersdc./driverdc."
        )
    if db in cena_sql_schema.SCHEMA_ALIASES:
        key = f"{db}.{name}"
        if key in allowlist:
            return True, key, ""
        return False, None, (
            f"table '{key}' is not allowlisted (it may be excluded by data-hygiene "
            f"policy). Available {db} tables: {_schema_tables(allowlist, db)}"
        )
    return False, None, (
        f"unknown schema '{db}' in table '{db}.{name}'; valid schemas are "
        f"appdb, toast, toastdm, ordersdc, driverdc (analytics tables are unqualified)."
    )


def _check_member(key: str, name: str, allowlist: dict, excluded: dict) -> str:
    """'' when column `name` is allowed on table `key`, else a rejection reason."""
    cols = allowlist[key]
    if name in cols:
        return ""
    if name in excluded.get(key, frozenset()):
        return (
            f"column '{name}' on '{key}' is excluded by policy (PII / auth / "
            f"individual pay). Allowed columns: {_fmt_cols(cols)}"
        )
    return (
        f"column '{name}' does not exist on '{key}'. Allowed columns: {_fmt_cols(cols)}"
    )


def validate_sql(sql: str) -> tuple[bool, str]:
    if not isinstance(sql, str) or not sql.strip():
        return False, "empty SQL; submit exactly one read-only SELECT statement."

    tok = _leading_keyword(sql)
    if tok not in ("select", "with"):
        return False, (
            f"only SELECT queries are allowed, but the statement starts with "
            f"'{tok.upper()}'. Write a single read-only SELECT (WITH ... SELECT is ok); "
            f"INSERT/UPDATE/DELETE/DDL/PRAGMA/ATTACH are forbidden."
        )

    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except ParseError as e:
        return False, f"SQL parse error: {str(e).splitlines()[0]}"
    statements = [s for s in statements if s is not None]
    if not statements:
        return False, "empty SQL; submit exactly one read-only SELECT statement."
    if len(statements) > 1:
        return False, (
            f"multiple SQL statements detected ({len(statements)}); submit exactly "
            f"one SELECT statement without semicolons between statements."
        )
    root = statements[0]

    if not isinstance(root, _ALLOWED_ROOTS):
        return False, (
            f"only SELECT statements are allowed (got {type(root).__name__.upper()}). "
            f"Write a single read-only SELECT."
        )
    for node in root.find_all(*_FORBIDDEN_NODES):
        return False, (
            f"forbidden statement element '{type(node).__name__.upper()}' detected; "
            f"only read-only SELECT is allowed."
        )
    for sel in root.find_all(exp.Select):
        if sel.args.get("into") is not None:
            return False, (
                "SELECT ... INTO is not allowed; results may not be written "
                "anywhere. Use a plain SELECT."
            )

    for fn in root.find_all(exp.Func):
        names = {fn.sql_name().lower()}
        if isinstance(fn, exp.Anonymous):
            names.add((fn.name or "").lower())
        hit = names & _DANGEROUS_FUNCS
        if hit:
            return False, f"function '{sorted(hit)[0]}' is not allowed."

    allowlist = cena_sql_schema.get_allowlist()
    excluded = cena_sql_schema.get_excluded_columns()
    analytics = {k for k in allowlist if "." not in k}

    try:
        scopes = traverse_scope(root)
    except Exception as e:  # fail CLOSED - this is the safety gate
        return False, (
            f"could not analyze query structure ({type(e).__name__}); simplify the "
            f"query to a plain SELECT with explicit tables."
        )

    # Pass 1: resolve every source in every scope. _OPAQUE = CTE/subquery/json_each.
    scope_sources: list[dict[str, str | None]] = []
    global_tables: dict[str, str] = {}
    global_opaque: set[str] = set()
    for scope in scopes:
        local: dict[str, str | None] = {}
        for alias, src in scope.sources.items():
            a = (alias or "").lower()
            if isinstance(src, exp.Table):
                ok, key, reason = _resolve_table(src, allowlist, analytics)
                if not ok:
                    return False, reason
                local[a] = key
                if key is _OPAQUE:
                    global_opaque.add(a)
                else:
                    global_tables[a] = key
            else:  # Scope (CTE or derived table)
                local[a] = _OPAQUE
                global_opaque.add(a)
        scope_sources.append(local)

    # Pass 2: columns + stars per scope. Scopes come innermost-first, and a column
    # node can be listed in an outer scope too (sqlglot quirk with scalar
    # subqueries) - dedupe by node identity so each column is judged in its
    # innermost (correct) scope.
    seen: set[int] = set()
    for scope, local in zip(scopes, scope_sources):
        real_local = sorted({k for k in local.values() if k})
        has_opaque = any(v is _OPAQUE for v in local.values())
        # Output names that ORDER BY/GROUP BY/HAVING may legally reference.
        # Inside a Select scope only EXPLICIT aliases count (a bare projection
        # column like `SELECT tips ...` must still pass the allowlist check).
        # In set-operation scopes (UNION ...) ORDER BY can only reference output
        # columns, so the full projection name list applies.
        if isinstance(scope.expression, exp.Select):
            out_names = {
                (item.alias or "").lower()
                for item in scope.expression.selects
                if isinstance(item, exp.Alias)
            } - {""}
        else:
            out_names = {
                (n or "").lower()
                for n in (getattr(scope.expression, "named_selects", None) or [])
            } - {"", "*"}

        # --- star projections ---
        if isinstance(scope.expression, exp.Select):
            for item in scope.expression.selects:
                star_qual = None
                if isinstance(item, exp.Star):
                    targets = real_local
                elif isinstance(item, exp.Column) and isinstance(item.this, exp.Star):
                    star_qual = (item.table or "").lower()
                    if star_qual in local:
                        key = local[star_qual]
                    elif star_qual in global_tables:
                        key = global_tables[star_qual]
                    elif star_qual in global_opaque:
                        key = _OPAQUE
                    else:
                        return False, (
                            f"unknown table or alias '{star_qual}' in "
                            f"'{star_qual}.*'; declare it in FROM first."
                        )
                    targets = [] if key is _OPAQUE else [key]
                else:
                    continue
                for key in targets:
                    if "." not in key:
                        continue  # analytics: SELECT * allowed
                    if excluded.get(key):
                        return False, (
                            f"SELECT {(star_qual + '.') if star_qual else ''}* is not "
                            f"allowed on '{key}' because it carries excluded columns. "
                            f"Select explicit columns from: {_fmt_cols(allowlist[key])}"
                        )

        # --- explicit column references ---
        for col in scope.columns:
            if id(col) in seen:
                continue
            seen.add(id(col))
            if isinstance(col.this, exp.Star):
                continue  # handled above
            name = (col.name or "").lower()
            if not name or name in _IMPLICIT_COLS:
                continue
            qual = (col.table or "").lower()
            cdb = (col.db or "").lower()
            if cdb:  # fully qualified schema.table.column
                key = f"{cdb}.{qual}"
                if key not in allowlist:
                    return False, (
                        f"table '{key}' (referenced by column '{key}.{name}') is not "
                        f"allowlisted."
                    )
                reason = _check_member(key, name, allowlist, excluded)
                if reason:
                    return False, reason
                continue
            if qual:
                if qual in local:
                    key = local[qual]
                elif qual in global_tables:  # correlated reference to outer scope
                    key = global_tables[qual]
                elif qual in global_opaque:
                    key = _OPAQUE
                else:
                    return False, (
                        f"unknown table or alias '{qual}' for column "
                        f"'{qual}.{name}'; declare it in FROM/JOIN first."
                    )
                if key is _OPAQUE:
                    continue  # CTE/subquery output - shape unknown by design
                reason = _check_member(key, name, allowlist, excluded)
                if reason:
                    return False, reason
                continue
            # unqualified column
            if name in out_names:
                continue  # references a SELECT output alias (ORDER BY/GROUP BY)
            cands = sorted(k for k in real_local if name in allowlist[k])
            if len(cands) == 1:
                continue
            if len(cands) == 0:
                # SECURITY (VAL-001): an unqualified name that matches an EXCLUDED
                # column of any real allowlisted table in scope must ALWAYS be
                # rejected, before the has_opaque / correlated-ancestor allowances.
                # Otherwise a no-op opaque source (a CTE, derived table, or
                # json_each) in the FROM lets an excluded PII / pay / auth column
                # be read unqualified and bypass the column gate entirely.
                excl_hits = sorted(
                    k for k in real_local if name in excluded.get(k, frozenset())
                )
                if excl_hits:
                    return False, (
                        f"column '{name}' is excluded by policy (PII / auth / "
                        f"individual pay) on {', '.join(excl_hits)}. Allowed columns "
                        f"on {excl_hits[0]}: {_fmt_cols(allowlist[excl_hits[0]])}"
                    )
                if has_opaque:
                    continue  # may come from a CTE/subquery source
                anc = sorted({k for k in global_tables.values() if name in allowlist[k]})
                if len(anc) == 1:
                    continue  # correlated unqualified reference, unambiguous
                holders = sorted(k for k in allowlist if name in allowlist[k])[:4]
                hint = (
                    f" It exists on: {', '.join(holders)}." if holders else ""
                )
                return False, (
                    f"column '{name}' was not found in the referenced tables "
                    f"({', '.join(real_local) or 'none'}).{hint}"
                )
            return False, (
                f"unqualified column '{name}' is ambiguous-or-excluded; it exists in "
                f"multiple referenced tables: {', '.join(cands)}. Qualify it, e.g. "
                f"'{cands[0].split('.')[-1]}.{name}'."
            )

    return True, ""
