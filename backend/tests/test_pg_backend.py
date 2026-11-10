"""PostgreSQL backend tests: dialect conversion (always) + live PG (opt-in).

The dialect/wrapper unit tests run everywhere with a fake psycopg connection
— no database needed. The ``LivePG`` class runs the full bootstrap against a
real PostgreSQL server when ``EVOFLOW_DB_URL`` is set (CI provides one via a
service container; locally unset = skipped).

Run locally:  cd backend && uv run pytest tests/test_pg_backend.py -v
Run live:     EVOFLOW_DB_URL=postgresql://u:p@localhost:5432/evoflow_test \
                uv run pytest tests/test_pg_backend.py -v -k LivePG
"""

from __future__ import annotations

import os
import re
import sqlite3

import pytest

from evoflow.persistence import db_backend as pg
from evoflow.persistence import pg_ddl


# ---------------------------------------------------------------------------
# Fakes (minimal psycopg surface)
# ---------------------------------------------------------------------------


class FakeInfo:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn

    @property
    def transaction_status(self) -> int:
        return self._conn.status


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self.description = None
        self.rowcount = -1
        self._rows: list = []
        self._pos = 0

    def execute(self, sql: str, params=()) -> None:
        self._conn.log.append(("exec", sql, params))
        self.description, self._rows = self._conn.respond(sql, params)
        self._pos = 0
        self.rowcount = len(self._rows) if self.description else -1

    def executemany(self, sql: str, seq) -> None:
        rows = list(seq)
        self._conn.log.append(("many", sql, rows))
        self.description = None
        self._rows = []
        self._pos = 0
        self.rowcount = len(rows)

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self):
        out = list(self._rows[self._pos :])
        self._pos = len(self._rows)
        return out

    def close(self) -> None:
        pass


class FakeConn:
    """Minimal psycopg connection with canned per-statement responses."""

    _lastval = 42

    def __init__(self) -> None:
        self.log: list = []
        self.meta: dict[str, str] = {}
        self.status = 0
        self.info = FakeInfo(self)
        self.closed = False
        self.tables: dict[str, list] = {
            "evoflow_chat_sessions": [
                ("id", "text", "NO", None),
                ("created_by", "text", "YES", None),
            ],
        }
        self.pk: dict[str, list[str]] = {"evoflow_chat_sessions": ["id"]}

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.log.append(("commit",))
        self.status = 0

    def rollback(self) -> None:
        self.log.append(("rollback",))
        self.status = 0

    def close(self) -> None:
        self.closed = True

    def respond(self, sql: str, params):
        s = sql.strip()
        if s == "SELECT lastval()":
            return [("lastval",)], [(self._lastval,)]
        if "information_schema.columns" in s:
            return [("column_name", "data_type", "is_nullable", "column_default")], self.tables.get(params[0], [])
        if "pg_index" in s:
            return [("attname",)], [(c,) for c in self.pk.get(params[0], [])]
        if "evoflow_schema_meta" in s and s.upper().startswith("SELECT"):
            if "user_version" in self.meta:
                return [("value",)], [(self.meta["user_version"],)]
            return [("value",)], []
        if "evoflow_schema_meta" in s and s.upper().startswith("INSERT"):
            self.meta["user_version"] = params[0]
            return None, []
        if s.upper().startswith("SET ") or s.startswith("CREATE TABLE"):
            return None, []
        if s.upper().startswith(("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE")):
            if s.upper().startswith(("BEGIN", "SAVEPOINT")):
                self.status = 2
            return None, []
        return [("id",), ("name",)], [(1, "alice"), (2, "bob")]


# ---------------------------------------------------------------------------
# translate_placeholders
# ---------------------------------------------------------------------------


def test_placeholders_basic():
    assert (
        pg.translate_placeholders("SELECT * FROM t WHERE a = ? AND b = ?")
        == "SELECT * FROM t WHERE a = %s AND b = %s"
    )


def test_placeholders_in_string_literal():
    assert pg.translate_placeholders("SELECT '?' WHERE a = ?") == "SELECT '?' WHERE a = %s"


def test_placeholders_escaped_quote():
    assert pg.translate_placeholders("SELECT 'it''s ?' WHERE a = ?") == "SELECT 'it''s ?' WHERE a = %s"


def test_placeholders_quoted_identifier():
    assert pg.translate_placeholders('SELECT "col?" FROM t WHERE a = ?') == 'SELECT "col?" FROM t WHERE a = %s'


def test_placeholders_comments():
    assert pg.translate_placeholders("SELECT 1 -- ? c\n, ?") == "SELECT 1 -- ? c\n, %s"
    assert pg.translate_placeholders("SELECT /* ? */ ?") == "SELECT /* ? */ %s"


# ---------------------------------------------------------------------------
# translate_sql
# ---------------------------------------------------------------------------


def test_translate_begin():
    assert pg.translate_sql("BEGIN IMMEDIATE") == "BEGIN"
    assert pg.translate_sql("BEGIN") == "BEGIN"


def test_translate_insert_or_ignore():
    assert (
        pg.translate_sql("INSERT OR IGNORE INTO kb_folders (id, name) VALUES (?, ?)")
        == "INSERT INTO kb_folders (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING"
    )
    assert pg.translate_sql("INSERT OR IGNORE INTO t (a) VALUES (?);").endswith("ON CONFLICT DO NOTHING;")
    assert (
        pg.translate_sql("INSERT OR IGNORE INTO t (a) VALUES (?) RETURNING id")
        == "INSERT INTO t (a) VALUES (%s) ON CONFLICT DO NOTHING RETURNING id"
    )
    assert "ON CONFLICT DO NOTHING" in pg.translate_sql("insert or ignore into t (a) values (?)")


# ---------------------------------------------------------------------------
# PgRow
# ---------------------------------------------------------------------------


def test_pgrow_surface():
    r = pg.PgRow(("id", "name"), (7, "alice"))
    assert r.keys() == ["id", "name"]
    assert r["name"] == "alice"
    assert r[0] == 7
    assert r[-1] == "alice"
    assert len(r) == 2
    assert list(r) == [7, "alice"]
    assert r == (7, "alice")
    with pytest.raises(IndexError):
        r["missing"]


# ---------------------------------------------------------------------------
# is_retryable_db_error
# ---------------------------------------------------------------------------


class FakePgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"pg error {sqlstate}")
        self.sqlstate = sqlstate


def test_retryable_sqlite():
    assert pg.is_retryable_db_error(sqlite3.OperationalError("database is locked"))
    assert pg.is_retryable_db_error(sqlite3.OperationalError("database is busy"))
    assert not pg.is_retryable_db_error(sqlite3.OperationalError("no such table"))


def test_retryable_pg():
    for state in ("40001", "40P01", "55P03"):
        assert pg.is_retryable_db_error(FakePgError(state))
    assert not pg.is_retryable_db_error(FakePgError("23505"))
    assert not pg.is_retryable_db_error(ValueError("x"))


# ---------------------------------------------------------------------------
# PostgresConnection wrapper (fake conn)
# ---------------------------------------------------------------------------


def test_wrapper_select_and_rows():
    conn = FakeConn()
    w = pg.PostgresConnection(conn)
    cur = w.execute("SELECT id, name FROM t WHERE a = ? AND b = ?", (1, "x"))
    assert conn.log[-1][1] == "SELECT id, name FROM t WHERE a = %s AND b = %s"
    assert conn.log[-1][2] == (1, "x")
    rows = cur.fetchall()
    assert isinstance(rows[0], pg.PgRow)
    assert rows[0].keys() == ["id", "name"]
    assert rows[0]["name"] == "alice"
    assert cur.fetchone() is None


def test_wrapper_pragma_table_info():
    conn = FakeConn()
    w = pg.PostgresConnection(conn)
    rows = w.execute("PRAGMA table_info(evoflow_chat_sessions)").fetchall()
    cols = {str(r[1]) for r in rows}
    assert cols == {"id", "created_by"}
    row_id = [r for r in rows if r[1] == "id"][0]
    assert row_id[5] == 1  # pk
    assert row_id[3] == 1  # notnull
    # quoted variant
    assert w.execute('PRAGMA table_info("evoflow_chat_sessions")').fetchall()[0][1] == "id"


def test_wrapper_user_version_roundtrip():
    conn = FakeConn()
    w = pg.PostgresConnection(conn)
    w.execute("PRAGMA user_version = 7")
    assert conn.meta.get("user_version") == "7"
    assert w.execute("PRAGMA user_version").fetchone()[0] == 7


def test_wrapper_transactions():
    conn = FakeConn()
    w = pg.PostgresConnection(conn)
    w.execute("BEGIN IMMEDIATE")
    assert conn.log[-1][1] == "BEGIN"
    assert w.in_transaction is True
    w.commit()
    assert w.in_transaction is False
    w.execute("SAVEPOINT evoflow_sp_1")
    w.execute("RELEASE SAVEPOINT evoflow_sp_1")
    w.rollback()
    assert conn.log[-1] == ("rollback",)


def test_wrapper_insert_lastrowid_and_bool():
    conn = FakeConn()
    w = pg.PostgresConnection(conn)
    conn._lastval = 99
    cur = w.execute("INSERT OR IGNORE INTO kb_folders (id, name) VALUES (?, ?)", ("f1", "root"))
    inserts = [e for e in conn.log if e[0] == "exec" and e[1].startswith("INSERT INTO kb_folders")]
    assert inserts[-1][1].endswith("ON CONFLICT DO NOTHING")
    assert cur.lastrowid == 99
    w.execute("INSERT INTO t (flag) VALUES (?)", (True,))
    flag = [e for e in conn.log if e[0] == "exec" and e[1].startswith("INSERT INTO t (flag)")]
    assert flag[-1][2] == (1,)


def test_wrapper_executemany_and_pragma_noop():
    conn = FakeConn()
    w = pg.PostgresConnection(conn)
    w.executemany("INSERT INTO t (a, b) VALUES (?, ?)", [("x", True), ("y", False)])
    kind, sql, rows = conn.log[-1]
    assert kind == "many"
    assert sql == "INSERT INTO t (a, b) VALUES (%s, %s)"
    assert rows == [("x", 1), ("y", 0)]
    assert w.execute("PRAGMA busy_timeout=10000").fetchall() == []


def test_wrapper_executescript():
    conn = FakeConn()
    w = pg.PostgresConnection(conn)
    conn.log.clear()
    w.executescript("CREATE TABLE t (a TEXT); INSERT INTO t (a) VALUES ('?');")
    kinds = [e[0] for e in conn.log]
    assert kinds[0] == "commit" and kinds[-1] == "commit"
    script_exec = [e for e in conn.log if e[0] == "exec"][0]
    assert "'?'" in script_exec[1]  # literal ? untouched by placeholder scanner


# ---------------------------------------------------------------------------
# pg_ddl conversion
# ---------------------------------------------------------------------------


def test_split_statements():
    assert len(pg_ddl.split_statements("CREATE TABLE a (x TEXT); -- ; c\nCREATE TABLE b (y TEXT);")) == 2
    assert len(pg_ddl.split_statements("INSERT INTO t VALUES ('a;b'); INSERT INTO t VALUES ('c')")) == 2
    assert pg_ddl.split_statements("") == []


def test_convert_ddl_types():
    out = pg_ddl.convert_ddl(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, n INTEGER NOT NULL DEFAULT 0,"
        " r REAL, b BLOB, name TEXT)"
    )
    assert "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" in out
    assert "n BIGINT NOT NULL DEFAULT 0" in out
    assert "DOUBLE PRECISION" in out
    assert "BYTEA" in out
    assert "name TEXT" in out


def test_convert_ddl_literal_protection():
    out = pg_ddl.convert_ddl("CREATE TABLE t (x TEXT DEFAULT 'INTEGER', y INTEGER)")
    assert "'INTEGER'" in out
    assert "y BIGINT" in out


def test_convert_ddl_composite_pk_untouched():
    out = pg_ddl.convert_ddl("CREATE TABLE t (a TEXT, b TEXT, PRIMARY KEY (a, b))")
    assert "PRIMARY KEY (a, b)" in out
    assert "IDENTITY" not in out


def test_convert_strftime():
    out = pg_ddl.convert_statement("INSERT INTO t (ts) VALUES (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))")
    assert out == "INSERT INTO t (ts) VALUES (to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'))"
    out = pg_ddl.convert_statement("UPDATE t SET updated_at = strftime('%Y-%m-%d %H:%M:%S','now') WHERE id = 1")
    assert "'YYYY-MM-DD HH24:MI:SS'" in out


def test_convert_insert_or_replace_raises():
    with pytest.raises(NotImplementedError, match="evoflow_apps"):
        pg_ddl.convert_statement("INSERT OR REPLACE INTO evoflow_apps (id) VALUES (?)")


def test_convert_ddl_with_leading_comment():
    # statements documented with -- headers must still be converted
    out = pg_ddl.convert_statement("-- doc comment\nCREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    assert "GENERATED BY DEFAULT AS IDENTITY" in out


# ---------------------------------------------------------------------------
# Full baseline conversion (the real schema)
# ---------------------------------------------------------------------------


def test_baseline_conversion_end_to_end():
    from evoflow.persistence import schema as schema_mod

    converted = pg_ddl.convert_script(schema_mod._BASELINE_DDL)
    joined = ";\n".join(converted)
    assert "AUTOINCREMENT" not in joined.upper()
    assert "strftime" not in joined.lower()
    assert not re.search(r"\bINTEGER\b", joined)
    assert joined.count("GENERATED BY DEFAULT AS IDENTITY") == schema_mod._BASELINE_DDL.count("AUTOINCREMENT")

    # FK deferral (PG validates references at DDL time; SQLite does not)
    inline = [s for s in converted if s.upper().startswith("CREATE TABLE") and "FOREIGN KEY" in s.upper()]
    assert not inline
    alters = [s for s in converted if s.upper().startswith("ALTER TABLE") and "FOREIGN KEY" in s.upper()]
    assert len(alters) == len(re.findall(r"(?i)FOREIGN\s+KEY", schema_mod._BASELINE_DDL))
    last_create = max(i for i, s in enumerate(converted) if s.upper().startswith("CREATE TABLE"))
    first_alter = min(i for i, s in enumerate(converted) if "FOREIGN KEY" in s.upper() and s.upper().startswith("ALTER"))
    assert first_alter > last_create


# ---------------------------------------------------------------------------
# Live PostgreSQL integration (opt-in via EVOFLOW_DB_URL)
# ---------------------------------------------------------------------------

PG_URL = (os.getenv("EVOFLOW_DB_URL") or "").strip()


@pytest.fixture(scope="class")
def live_pg():
    from evoflow.persistence import db as db_mod

    db_mod.reset_db_for_tests()
    yield db_mod
    db_mod.reset_db_for_tests()


@pytest.mark.skipif(not PG_URL, reason="EVOFLOW_DB_URL not set (no live PG)")
@pytest.mark.usefixtures("live_pg")
class TestLivePG:
    """Full bootstrap + CRUD against a real PostgreSQL server."""

    def test_bootstrap_and_schema_version(self, live_pg):
        db = live_pg.get_db()
        version = live_pg.app_schema_version()
        assert version >= 1

    def test_pragma_table_info_intercepted(self, live_pg):
        db = live_pg.get_db()
        cols = {str(r[1]) for r in db.execute("PRAGMA table_info(evoflow_models)").fetchall()}
        assert "owner_principal_id" in cols
        assert "name" in cols

    def test_insert_lastrowid_via_lastval(self, live_pg):
        db = live_pg.get_db()
        cur = db.execute(
            "INSERT INTO evoflow_proactive_roles (agent_code, role_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("pg_ci_agent", "CI Role", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
        )
        assert cur.lastrowid is not None and cur.lastrowid > 0

    def test_insert_or_ignore_translation(self, live_pg):
        db = live_pg.get_db()
        for _ in range(2):  # second insert conflicts → DO NOTHING
            db.execute(
                "INSERT OR IGNORE INTO evoflow_proactive_roles (agent_code, role_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                ("pg_ci_agent", "CI Role", "2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000Z"),
            )
        db.commit()
        row = db.execute(
            "SELECT COUNT(*) AS c FROM evoflow_proactive_roles WHERE agent_code = 'pg_ci_agent'"
        ).fetchone()
        assert row["c"] == 1

    def test_upsert_on_conflict(self, live_pg):
        db = live_pg.get_db()
        db.execute(
            "CREATE TABLE IF NOT EXISTS evoflow_usage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for val in ("first", "second"):
            db.execute(
                "INSERT INTO evoflow_usage_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("pg_ci_marker", val),
            )
        db.commit()
        row = db.execute("SELECT value FROM evoflow_usage_meta WHERE key = 'pg_ci_marker'").fetchone()
        assert row["value"] == "second"

    def test_transaction_nesting_savepoints(self, live_pg):
        db = live_pg.get_db()
        db_mod = live_pg

        def inner(d2):
            return d2.execute("SELECT 1 AS v").fetchone()["v"]

        def outer(db):
            nested = db_mod.run_db_transaction(inner)
            return nested + 1

        assert db_mod.run_db_transaction(outer) == 2

    def test_bool_params_coerced(self, live_pg):
        db = live_pg.get_db()
        db.execute(
            "CREATE TABLE IF NOT EXISTS evoflow_ci_bool_probe (k TEXT PRIMARY KEY, flag BIGINT NOT NULL DEFAULT 0)"
        )
        db.execute("INSERT INTO evoflow_ci_bool_probe (k, flag) VALUES (?, ?) "
                   "ON CONFLICT(k) DO UPDATE SET flag = excluded.flag", ("b", True))
        db.commit()
        row = db.execute("SELECT flag FROM evoflow_ci_bool_probe WHERE k = 'b'").fetchone()
        assert row["flag"] == 1
