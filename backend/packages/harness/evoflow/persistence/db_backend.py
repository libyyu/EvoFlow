"""Database backend abstraction: SQLite dialect translation + PostgreSQL support.

The whole persistence layer (116+ files) is written against the ``sqlite3``
DB-API surface and the SQLite SQL dialect. Instead of touching every call
site, PostgreSQL support is provided as a drop-in wrapper around a single
psycopg3 connection:

- :class:`PostgresConnection` mimics the ``_LockedConnection`` surface used by
  :mod:`evoflow.persistence.db` (``execute`` / ``executemany`` /
  ``executescript`` / ``commit`` / ``rollback`` / ``in_transaction`` /
  ``close``), guarded by the same per-connection ``RLock`` pattern, so
  ``run_db_transaction``'s BEGIN / SAVEPOINT protocol works unchanged
  (``BEGIN IMMEDIATE`` is rewritten to ``BEGIN``).
- :func:`translate_placeholders` converts ``?`` to ``%s`` with a safe scanner
  that skips string literals, quoted identifiers and comments.
- :func:`translate_sql` rewrites SQLite-specific statements
  (``INSERT OR IGNORE`` → ``ON CONFLICT DO NOTHING``, ``BEGIN IMMEDIATE`` →
  ``BEGIN``).
- ``PRAGMA table_info(t)`` / ``PRAGMA user_version`` are intercepted in
  ``execute()`` and answered from ``information_schema`` / a small version
  table, so the ~60 business call sites work unchanged.
- :class:`PgRow` is a ``sqlite3.Row``-compatible row (``keys()``, column-name
  indexing, integer indexing, iteration). ``cursor.lastrowid`` is emulated
  eagerly via ``SELECT lastval()`` so the 9 call sites keep working.

Activation: set ``EVOFLOW_DB_URL=postgresql://user:pass@host:port/dbname``.
When unset (the default — local single-user mode) this module is never used
by the hot path; SQLite remains the default backend with zero behavior
change.

Phase-1 limitations (documented, by design):
- One connection per process, all statements serialized by the RLock — the
  same concurrency model as the SQLite backend. Multi-worker scaling comes
  from running multiple gateway workers, each with its own connection.
- ``run_db_read`` uses the shared connection (no separate read pool).
- The observability / checkpoints databases remain SQLite files per worker.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def resolve_db_url() -> str:
    """Return the PostgreSQL URL from ``EVOFLOW_DB_URL`` (empty = SQLite mode).

    Accepts either a bare connection string (``host:port/db`` style is NOT
    supported; a value without ``://`` is assumed to be a full URL minus the
    scheme) or a full ``postgresql://`` / ``postgres://`` URL.
    """
    raw = (os.getenv("EVOFLOW_DB_URL") or "").strip()
    if raw and "://" not in raw:
        raw = f"postgresql://{raw}"
    return raw


def postgres_mode() -> bool:
    """True when the app should run on the PostgreSQL backend."""
    return bool(resolve_db_url())


# ---------------------------------------------------------------------------
# Row / cursor adapters
# ---------------------------------------------------------------------------


class PgRow:
    """A ``sqlite3.Row``-compatible row backed by a tuple + column names.

    Supports ``keys()``, indexing by column name (missing names raise
    ``IndexError`` like sqlite3.Row), integer indexing, ``len()``, iteration
    and equality against plain tuples.
    """

    __slots__ = ("_columns", "_data", "_index")

    def __init__(self, columns: Sequence[str], data: Sequence[Any]) -> None:
        self._columns = tuple(columns)
        self._data = tuple(data)
        self._index = {c: i for i, c in enumerate(self._columns)}

    def keys(self) -> list[str]:
        return list(self._columns)

    def __len__(self) -> int:
        return len(self._data)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            try:
                return self._data[self._index[key]]
            except KeyError:
                raise IndexError(f"No column named {key!r} in row") from None
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __eq__(self, other: Any) -> Any:
        if isinstance(other, PgRow):
            return self._data == other._data and self._columns == other._columns
        if isinstance(other, (tuple, list)):
            return self._data == tuple(other)
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]  # rows may contain unhashables

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "PgRow(" + ", ".join(f"{c}={v!r}" for c, v in zip(self._columns, self._data)) + ")"


class _SyntheticCursor:
    """Cursor-like object for intercepted statements answered without the DB.

    Mirrors the sqlite3 cursor surface consumers rely on:
    ``fetchone`` / ``fetchall`` / ``rowcount`` / ``description`` / ``close``.
    """

    def __init__(self, rows: Iterable[Any] = (), columns: Sequence[str] = ()) -> None:
        self._rows = list(rows)
        self._cols = tuple(columns)
        self.rowcount = len(self._rows)
        self.description = [(c, None, None, None, None, None, None) for c in self._cols] if self._cols else None
        self.arraysize = 1
        self.lastrowid = None
        self._pos = 0

    def fetchone(self) -> Any:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list[Any]:
        out = self._rows[self._pos :]
        self._pos = len(self._rows)
        return list(out)

    def fetchmany(self, size: int | None = None) -> list[Any]:
        n = self.arraysize if size is None else size
        out = self._rows[self._pos : self._pos + n]
        self._pos += len(out)
        return list(out)

    def __iter__(self):
        return iter(self._rows)

    def close(self) -> None:
        pass


class _PgCursor:
    """Wraps a psycopg cursor, converting rows to :class:`PgRow`."""

    __slots__ = ("_conn", "_cur", "_lastrowid", "arraysize")

    def __init__(self, conn: "PostgresConnection", cur: Any, lastrowid: Any = None) -> None:
        self._conn = conn
        self._cur = cur
        self._lastrowid = lastrowid
        self.arraysize = 1

    @property
    def description(self) -> Any:
        return self._cur.description

    @property
    def rowcount(self) -> int:
        return int(self._cur.rowcount or 0)

    @property
    def lastrowid(self) -> Any:
        return self._lastrowid

    def _names(self) -> list[str]:
        desc = self._cur.description
        if not desc:
            return []
        out: list[str] = []
        for d in desc:
            # psycopg3 Column (named tuple-like) or plain DB-API tuple
            name = getattr(d, "name", None)
            if name is None and isinstance(d, (tuple, list)) and d:
                name = d[0]
            out.append(str(name))
        return out

    def _wrap(self, row: Any) -> Any:
        if row is None:
            return None
        return PgRow(self._names(), row)

    def fetchone(self) -> Any:
        return self._wrap(self._cur.fetchone())

    def fetchall(self) -> list[Any]:
        return [self._wrap(r) for r in self._cur.fetchall()]

    def fetchmany(self, size: int | None = None) -> list[Any]:
        rows = self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()
        return [self._wrap(r) for r in rows]

    def __iter__(self):
        for r in self._cur:
            yield self._wrap(r)

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SQL translation
# ---------------------------------------------------------------------------


def translate_placeholders(sql: str) -> str:
    """Convert sqlite ``?`` placeholders to psycopg ``%s``.

    Safe scanner: ``?`` inside single-quoted strings (``''`` escape),
    double-quoted identifiers, ``--`` line comments and ``/* */`` block
    comments is left untouched.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out.append("''")
                        i += 2
                        continue
                    out.append("'")
                    i += 1
                    break
                out.append(sql[i])
                i += 1
            continue
        if ch == '"':
            out.append(ch)
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        out.append('""')
                        i += 2
                        continue
                    out.append('"')
                    i += 1
                    break
                out.append(sql[i])
                i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            if j == -1:
                out.append(sql[i:])
                break
            out.append(sql[i : j + 1])
            i = j + 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                out.append(sql[i:])
                break
            out.append(sql[i : j + 2])
            i = j + 2
            continue
        if ch == "?":
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_BEGIN_RE = re.compile(r"(?is)^\s*BEGIN\b")
_INSERT_OR_IGNORE_RE = re.compile(r"(?i)(?<![A-Za-z_])INSERT\s+OR\s+IGNORE(?=\s)")
_RETURNING_RE = re.compile(r"(?is)\bRETURNING\b")


def translate_sql(sql: str) -> str:
    """Rewrite SQLite-specific single-statement SQL to PostgreSQL.

    - ``BEGIN IMMEDIATE`` (any BEGIN variant) → ``BEGIN``
    - ``INSERT OR IGNORE INTO ...`` → ``INSERT INTO ... ON CONFLICT DO NOTHING``
      (appended before ``RETURNING`` if present)
    - ``?`` → ``%s`` placeholders

    Statements that need table-specific knowledge (``INSERT OR REPLACE``)
    are NOT translated and will surface as a server error — by design, so
    they get fixed at the call site instead of silently corrupting data.
    """
    if _BEGIN_RE.match(sql):
        return "BEGIN"
    if _INSERT_OR_IGNORE_RE.search(sql):
        body = _INSERT_OR_IGNORE_RE.sub("INSERT", sql, count=1)
        # Insert ON CONFLICT DO NOTHING before RETURNING (if any).
        m = _RETURNING_RE.search(body)
        suffix = ""
        if m is None:
            stripped = body.rstrip()
            if stripped.endswith(";"):
                suffix = ";"
                stripped = stripped[:-1].rstrip()
            body = stripped + " ON CONFLICT DO NOTHING" + suffix
        else:
            body = body[: m.start()] + "ON CONFLICT DO NOTHING " + body[m.start() :]
        return translate_placeholders(body)
    return translate_placeholders(sql)


def _adapt_params(parameters: Any) -> Any:
    """Coerce parameters for PG: bool → int (schema uses INTEGER flags)."""
    if isinstance(parameters, (list, tuple)):
        return type(parameters)(  # keep list/tuple type
            (int(v) if isinstance(v, bool) else v) for v in parameters
        )
    return parameters


def is_retryable_db_error(exc: BaseException) -> bool:
    """True for transient errors both backends can retry.

    - SQLite: ``database is locked`` / ``database is busy`` OperationalError
    - PostgreSQL: serialization failure (40001), deadlock detected (40P01),
      lock not available (55P03) — checked via the ``sqlstate`` attribute
      psycopg3 puts on all its exceptions.
    """
    import sqlite3

    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        return "locked" in msg or "busy" in msg
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate in ("40001", "40P01", "55P03"):
        return True
    if isinstance(exc, Exception):
        msg = str(exc).lower()
        if "could not serialize access" in msg or "deadlock detected" in msg:
            return True
    return False


# ---------------------------------------------------------------------------
# PRAGMA interception
# ---------------------------------------------------------------------------

_TABLE_INFO_RE = re.compile(
    r"(?is)^PRAGMA\s+table_info\(\s*"
    r"(?:'([^']+)'|\"([^\"]+)\"|\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_]*))"
    r"\s*\)\s*;?\s*$"
)
_USER_VERSION_RE = re.compile(r"(?is)^PRAGMA\s+user_version\s*$")
_USER_VERSION_SET_RE = re.compile(r"(?is)^PRAGMA\s+user_version\s*=\s*(\d+)\s*;?\s*$")

_SCHEMA_META_DDL = """
CREATE TABLE IF NOT EXISTS evoflow_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""

_TABLE_INFO_COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_name = %s AND table_schema = current_schema()
ORDER BY ordinal_position
"""

_TABLE_INFO_PK_SQL = """
SELECT a.attname
FROM pg_index i
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
WHERE i.indisprimary
  AND i.indrelid = to_regclass(current_schema() || '.' || %s)
"""


def _unquote_table(name: str) -> str:
    return name.strip().strip("'\"[]")


# ---------------------------------------------------------------------------
# The PostgreSQL connection wrapper
# ---------------------------------------------------------------------------


class PostgresConnection:
    """Drop-in replacement for the SQLite ``_LockedConnection``.

    All access is serialized by an RLock (same model as SQLite backend).
    ``execute`` performs dialect translation and PRAGMA interception before
    hitting psycopg. ``executescript`` sends the whole (translated) script in
    one round trip — psycopg allows multiple statements when no parameters
    are bound, which also keeps trigger bodies with embedded ``;`` intact.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._lock = threading.RLock()
        self._row_factory: Any = None
        self._schema_meta_ready = False

    # -- locking ------------------------------------------------------------
    @property
    def connection_lock(self) -> threading.RLock:
        return self._lock

    # -- row_factory (accepted, ignored: rows are always PgRow) -------------
    @property
    def row_factory(self) -> Any:
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._row_factory = value

    # -- execution ----------------------------------------------------------
    def execute(self, sql: str, parameters: Any = (), /) -> Any:
        with self._lock:
            return self._execute_locked(sql, parameters)

    def _execute_locked(self, sql: str, parameters: Any) -> Any:
        synthetic = self._intercept_pragma(sql, parameters)
        if synthetic is not None:
            return synthetic

        translated = self._convert(sql)
        params = _adapt_params(parameters)
        cur = self._conn.cursor()
        is_insert = bool(re.match(r"(?is)\s*INSERT\b", sql))
        cur.execute(translated, params)
        lastrowid = None
        if is_insert:
            # Eagerly capture the sequence value under the same lock so
            # cursor.lastrowid cannot race with another thread's INSERT.
            try:
                lv = self._conn.cursor()
                lv.execute("SELECT lastval()")
                row = lv.fetchone()
                if row and row[0] is not None:
                    lastrowid = int(row[0])
                lv.close()
            except Exception:
                lastrowid = None
        return _PgCursor(self, cur, lastrowid=lastrowid)

    @staticmethod
    def _convert(sql: str) -> str:
        """Full SQLite → PG conversion for one statement."""
        from evoflow.persistence.pg_ddl import convert_statement

        return translate_sql(convert_statement(sql))

    def executemany(self, sql: str, parameters: Any) -> Any:
        with self._lock:
            translated = self._convert(sql)
            rows = [_adapt_params(p) for p in parameters]
            cur = self._conn.cursor()
            cur.executemany(translated, rows)
            return _PgCursor(self, cur)

    def executescript(self, sql_script: str) -> Any:
        with self._lock:
            from evoflow.persistence.pg_ddl import convert_script

            # sqlite3.executescript commits any pending transaction first.
            self._conn.commit()
            statements = [translate_sql(s) for s in convert_script(sql_script)]
            script = ";\n".join(statements) + (";" if statements else "")
            cur = self._conn.cursor()
            # No parameters bound → psycopg uses the simple query protocol,
            # which accepts multiple statements in one round trip.
            if statements:
                cur.execute(script)
                cur.close()
            self._conn.commit()
            return _SyntheticCursor()

    # -- transaction control --------------------------------------------------
    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    @property
    def in_transaction(self) -> bool:
        with self._lock:
            try:
                status = self._conn.info.transaction_status
            except Exception:
                return False
            # 0 = IDLE, 1 = ACTIVE, 2 = INTRANS, 3 = INERROR, 4 = UNKNOWN
            return int(status) not in (0, 4)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # -- raw access for maintenance paths -------------------------------------
    def raw_connection(self) -> Any:
        """The underlying psycopg connection (already lock-guarded upstream)."""
        return self._conn

    # -- pragma interception ----------------------------------------------------
    def _intercept_pragma(self, sql: str, parameters: Any) -> Any:
        s = sql.strip()
        if not s.upper().startswith("PRAGMA"):
            return None

        m = _TABLE_INFO_RE.match(s)
        if m:
            table = _unquote_table(m.group(1) or m.group(2) or m.group(3) or m.group(4) or "")
            return self._pragma_table_info(table)

        m = _USER_VERSION_SET_RE.match(s)
        if m:
            self._ensure_schema_meta()
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO evoflow_schema_meta (key, value) VALUES ('user_version', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (str(int(m.group(1))),),
            )
            cur.close()
            return _SyntheticCursor()

        if _USER_VERSION_RE.match(s):
            self._ensure_schema_meta()
            cur = self._conn.cursor()
            cur.execute("SELECT value FROM evoflow_schema_meta WHERE key = 'user_version'")
            row = cur.fetchone()
            cur.close()
            version = int(row[0]) if row and row[0] is not None else 0
            return _SyntheticCursor([PgRow(("user_version",), (version,))], ("user_version",))

        # health-check pragmas → optimistic 'ok' (PG mode never runs the
        # sqlite preflight paths on the app DB; this only satisfies stray
        # callers)
        up = s.upper()
        if "INTEGRITY_CHECK" in up or "QUICK_CHECK" in up:
            return _SyntheticCursor([PgRow(("integrity_check",), ("ok",))], ("integrity_check",))

        # Everything else (busy_timeout, journal_mode, foreign_keys, ...) is a
        # SQLite engine setting with no PG equivalent → no-op.
        return _SyntheticCursor()

    def _pragma_table_info(self, table: str) -> Any:
        cur = self._conn.cursor()
        cur.execute(_TABLE_INFO_COLUMNS_SQL, (table,))
        cols = cur.fetchall()
        pk: set[str] = set()
        try:
            cur.execute(_TABLE_INFO_PK_SQL, (table,))
            pk = {str(r[0]) for r in cur.fetchall()}
        except Exception:
            pass
        cur.close()
        rows = [
            PgRow(
                ("cid", "name", "type", "notnull", "dflt_value", "pk"),
                (i, str(c[0]), str(c[1] or ""), 1 if str(c[2]) == "NO" else 0, c[3], 1 if str(c[0]) in pk else 0),
            )
            for i, c in enumerate(cols)
        ]
        return _SyntheticCursor(rows, ("cid", "name", "type", "notnull", "dflt_value", "pk"))

    def _ensure_schema_meta(self) -> None:
        if self._schema_meta_ready:
            return
        cur = self._conn.cursor()
        cur.execute(_SCHEMA_META_DDL)
        cur.close()
        self._conn.commit()
        self._schema_meta_ready = True


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------


def open_postgres_connection(url: str | None = None, *, ensure_schema: bool = True) -> PostgresConnection:
    """Open the process-wide PostgreSQL connection wrapper.

    Requires ``psycopg[binary] >= 3.1`` (lazy import with a clear error).
    Session settings applied:

    - ``lock_timeout = 15s`` — mirrors the SQLite ``busy_timeout`` semantics
      (bounded wait + retryable error instead of waiting forever).
    """
    url = url or resolve_db_url()
    if not url:
        raise RuntimeError(
            "PostgreSQL backend selected but EVOFLOW_DB_URL is not set. "
            "Set EVOFLOW_DB_URL=postgresql://user:pass@host:port/dbname "
            "or unset it to use the default SQLite backend."
        )
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on env
        raise RuntimeError(
            "PostgreSQL backend requires the 'psycopg[binary]' package. "
            "Install it with: pip install 'psycopg[binary]>=3.1' — or unset "
            "EVOFLOW_DB_URL to use the default SQLite backend."
        ) from exc

    timeout = max(1.0, float(os.getenv("EVOFLOW_PG_CONNECT_TIMEOUT_S", "10") or "10"))
    conn = psycopg.connect(url, autocommit=False, connect_timeout=int(timeout))
    wrapper = PostgresConnection(conn)
    try:
        cur = conn.cursor()
        cur.execute("SET lock_timeout = '15s'")
        cur.close()
        conn.commit()
        wrapper._ensure_schema_meta()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    if ensure_schema:
        from evoflow.persistence.schema import ensure_app_schema

        ensure_app_schema(wrapper)
    return wrapper
