"""SQLite → PostgreSQL statement/DDL conversion.

Applied by :mod:`evoflow.persistence.db_backend` before every statement hits
psycopg. Layering: this module handles *semantic* SQLite→PG transforms;
placeholder/``BEGIN``/``INSERT OR IGNORE`` glue stays in ``db_backend``.

Transforms
----------
- ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` → ``to_char(now() AT TIME ZONE
  'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')`` — timestamp format is preserved
  byte-for-byte because business code compares TEXT timestamps lexically.
- ``INTEGER PRIMARY KEY [AUTOINCREMENT]`` (column form) → ``BIGINT GENERATED
  BY DEFAULT AS IDENTITY PRIMARY KEY``.
- Type widening: ``INTEGER`` → ``BIGINT`` (SQLite INTEGER is 64-bit; ms-epoch
  values overflow int4), ``REAL`` → ``DOUBLE PRECISION``, ``BLOB`` → ``BYTEA``.
  Applied only outside string literals / comments / quoted identifiers.
- ``INSERT OR REPLACE`` is NOT translated (needs per-table conflict target +
  has delete-and-reinsert semantics that differ from upsert). It raises a
  clear error on PG so the call site gets rewritten to standard
  ``ON CONFLICT ... DO UPDATE`` (works on both backends).
"""

from __future__ import annotations

import re
from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Statement splitting (string / comment aware)
# ---------------------------------------------------------------------------


def split_statements(script: str) -> list[str]:
    """Split a SQL script on ``;`` outside strings / comments / identifiers.

    The EvoFlow schema contains no triggers, so semicolon splitting is safe.
    Each returned statement keeps its text (without the trailing ``;``);
    empty statements are dropped.
    """
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                if script[i] == "'":
                    if i + 1 < n and script[i + 1] == "'":
                        buf.append("''")
                        i += 2
                        continue
                    buf.append("'")
                    i += 1
                    break
                buf.append(script[i])
                i += 1
            continue
        if ch == '"':
            buf.append(ch)
            i += 1
            while i < n:
                if script[i] == '"':
                    if i + 1 < n and script[i + 1] == '"':
                        buf.append('""')
                        i += 2
                        continue
                    buf.append('"')
                    i += 1
                    break
                buf.append(script[i])
                i += 1
            continue
        if ch == "-" and i + 1 < n and script[i + 1] == "-":
            j = script.find("\n", i)
            if j == -1:
                buf.append(script[i:])
                break
            buf.append(script[i : j + 1])
            i = j + 1
            continue
        if ch == "/" and i + 1 < n and script[i + 1] == "*":
            j = script.find("*/", i + 2)
            if j == -1:
                buf.append(script[i:])
                break
            buf.append(script[i : j + 2])
            i = j + 2
            continue
        if ch == ";":
            out.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        out.append("".join(buf))
    return [s for s in (stmt.strip() for stmt in out) if s]


# ---------------------------------------------------------------------------
# Code-segment iteration (literal / comment aware transforms)
# ---------------------------------------------------------------------------


def _code_segments(sql: str) -> Iterator[tuple[bool, str]]:
    """Yield ``(is_code, text)`` segments; transforms apply to code only."""
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            else:
                j = n
            yield False, sql[i:j]
            i = j
            continue
        if ch == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            else:
                j = n
            yield False, sql[i:j]
            i = j
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            if j == -1:
                yield False, sql[i:]
                return
            yield False, sql[i : j + 1]
            i = j + 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                yield False, sql[i:]
                return
            yield False, sql[i : j + 2]
            i = j + 2
            continue
        # run of code
        j = i
        while j < n and sql[j] not in "'\"-/*":
            j += 1
        if j == i:
            # '-' / '/' / '*' as plain code chars (arithmetic etc.) —
            # consume one char so the scanner always advances
            j = i + 1
        yield True, sql[i:j]
        i = j


# ---------------------------------------------------------------------------
# strftime → to_char
# ---------------------------------------------------------------------------

_STRFTIME_RE = re.compile(r"(?i)\bstrftime\(\s*'((?:[^']|'')*)'\s*(?:,\s*'now'\s*)?\)")

_STRFTIME_CODES = {
    "Y": "YYYY",
    "m": "MM",
    "d": "DD",
    "H": "HH24",
    "M": "MI",
    "S": "SS",
    "f": "SS.MS",  # sqlite %f = seconds with fractional part
    "T": "HH24:MI:SS",
}


def _strftime_to_tochar_format(fmt: str) -> str:
    out: list[str] = []
    lit: list[str] = []

    # to_char passes these through literally without quoting; anything else
    # (letters/digits like T, Z) must be double-quoted.
    unquoted = set(".,:/- ()+")

    def flush() -> None:
        if lit:
            text = "".join(lit)
            if all(c in unquoted for c in text):
                out.append(text)
            else:
                out.append('"' + text.replace('"', '""') + '"')
            lit.clear()

    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch == "%" and i + 1 < len(fmt):
            code = fmt[i + 1]
            if code == "%":
                lit.append("%")
                i += 2
                continue
            mapped = _STRFTIME_CODES.get(code)
            if mapped is not None:
                flush()
                out.append(mapped)
                i += 2
                continue
            # unknown directive — keep verbatim; PG will fail loudly
            lit.append(ch)
            i += 1
            continue
        lit.append(ch)
        i += 1
    flush()
    return "".join(out)


def _replace_strftime(m: re.Match[str]) -> str:
    fmt = m.group(1).replace("''", "'")
    pgfmt = _strftime_to_tochar_format(fmt).replace("'", "''")
    return f"to_char(now() AT TIME ZONE 'UTC', '{pgfmt}')"


# ---------------------------------------------------------------------------
# DDL type transforms
# ---------------------------------------------------------------------------

_INTEGER_PK_AUTOINC = re.compile(r"(?i)\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b")
_INTEGER_PK = re.compile(r"(?i)\bINTEGER\s+PRIMARY\s+KEY\b")
_REAL = re.compile(r"(?i)\bREAL\b")
_BLOB = re.compile(r"(?i)\bBLOB\b")
_INTEGER = re.compile(r"(?i)\bINTEGER\b")
_IDENTITY = "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"

_DDL_VERBS = re.compile(r"(?i)^(CREATE|ALTER|DROP)\b")
_INSERT_OR_REPLACE = re.compile(r"(?is)INSERT\s+OR\s+REPLACE\b")


def _strip_leading_comments(sql: str) -> str:
    """Drop leading whitespace/comments so verb sniffing sees the real start.

    Schema statements are documented with ``-- comment`` headers; a naive
    ``^CREATE`` match would skip DDL conversion for those statements.
    """
    s = sql
    while True:
        s = s.lstrip()
        if s.startswith("--"):
            nl = s.find("\n")
            if nl == -1:
                return ""
            s = s[nl + 1 :]
            continue
        if s.startswith("/*"):
            end = s.find("*/")
            if end == -1:
                return ""
            s = s[end + 2 :]
            continue
        return s


def convert_ddl(sql: str) -> str:
    """Apply DDL type transforms outside literals/comments/identifiers."""
    out: list[str] = []
    for is_code, seg in _code_segments(sql):
        if not is_code:
            out.append(seg)
            continue
        seg = _INTEGER_PK_AUTOINC.sub(_IDENTITY, seg)
        seg = _INTEGER_PK.sub(_IDENTITY, seg)
        seg = _REAL.sub("DOUBLE PRECISION", seg)
        seg = _BLOB.sub("BYTEA", seg)
        seg = _INTEGER.sub("BIGINT", seg)
        out.append(seg)
    return "".join(out)


def convert_statement(sql: str) -> str:
    """Convert one SQLite statement (DML or DDL) to PostgreSQL semantics.

    Raises ``NotImplementedError`` for ``INSERT OR REPLACE`` — see module
    docstring; those call sites must be rewritten as upserts.
    """
    stripped = _strip_leading_comments(sql)
    if _INSERT_OR_REPLACE.match(stripped):
        # Fail loudly instead of silently corrupting data.
        table = re.match(
            r"(?is)INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", stripped
        )
        name = table.group(1) if table else "<unknown>"
        raise NotImplementedError(
            f"INSERT OR REPLACE is SQLite-only (table {name!r}). Rewrite as "
            "INSERT ... ON CONFLICT (<pk>) DO UPDATE SET ... — it works on "
            "both SQLite and PostgreSQL."
        )
    s = _STRFTIME_RE.sub(_replace_strftime, sql)
    if _DDL_VERBS.match(stripped):
        s = convert_ddl(s)
    return s


def convert_script(script: str) -> list[str]:
    """Split a script and convert each statement. Returns converted list.

    Foreign-key constraints are *deferred*: SQLite lets a CREATE TABLE
    reference a table defined later in the same script (FK resolution happens
    at DML time), but PostgreSQL validates references at DDL time. The
    baseline schema has ~10 forward references, so every inline ``FOREIGN
    KEY`` clause is extracted from CREATE TABLE statements and re-emitted as
    ``ALTER TABLE ... ADD CONSTRAINT ... FOREIGN KEY`` statements appended
    after all tables exist.
    """
    from_tables: list[str] = []  # (table, fk_clause) deferred

    _CREATE_TABLE_RE = re.compile(
        r"(?is)^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?"
    )
    _FK_RE = re.compile(
        r"(?is),?\s*FOREIGN\s+KEY\s*\([^)]*\)\s*REFERENCES\s+"
        r"\"?[A-Za-z_][A-Za-z0-9_]*\"?\s*(?:\([^)]*\))?"
        r"(?:\s+ON\s+DELETE\s+(?:CASCADE|SET\s+NULL|RESTRICT|NO\s+ACTION|SET\s+DEFAULT))?"
    )

    out: list[str] = []
    for stmt in split_statements(script):
        converted = convert_statement(stmt)
        m = _CREATE_TABLE_RE.match(_strip_leading_comments(stmt))
        if m:
            table = m.group(1)
            fks: list[str] = []

            def _take(mm: re.Match[str]) -> str:
                clause = mm.group(0).strip()
                if clause.startswith(","):
                    clause = clause[1:].strip()
                fks.append(clause)
                return ""

            stripped = _FK_RE.sub(_take, converted)
            # a removed mid-list clause can leave a trailing comma
            stripped = re.sub(r",\s*\)", ")", stripped)
            converted = stripped
            for i, fk in enumerate(fks):
                from_tables.append(f'ALTER TABLE "{table}" ADD CONSTRAINT "{table}_fk_deferred_{i}" {fk}')
        out.append(converted)
    return out + from_tables
