"""The database for the vendored engine: a SQLite file in the study's working folder.

The tables come from the studio's own schema file (schema/schema_consolidated.sql), translated
to SQLite the way the studio does it for its local mode, so every query the engine runs finds the
columns it expects. Same interface as the studio's helper: get_db().execute(sql, params) with
%s placeholders, returning a list of dicts; transaction() for a multi-statement unit. Python's
built-in sqlite3 only — no SQLAlchemy, no server.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

_SCHEMA = Path(__file__).resolve().parent / "schema" / "schema_consolidated.sql"
_SKIP_TABLES = ("sdtm_rag_chunks", "lead_review_domain", "lead_review_event")
_PATH: Path | None = None
_LOCK = threading.Lock()
_READY: set[str] = set()


def configure(path: Path) -> None:
    global _PATH
    _PATH = Path(path)
    _PATH.parent.mkdir(parents=True, exist_ok=True)


def _ddl() -> list[str]:
    """The studio's Postgres schema as SQLite statements (tables, then indexes)."""
    sql = re.sub(r"--[^\n]*", "", _SCHEMA.read_text(encoding="utf-8"))
    sql = re.sub(r"\$\$.*?\$\$", "", sql, flags=re.S)
    tables, indexes = [], []
    for stmt in (s.strip() for s in sql.split(";")):
        if any(re.search(rf"\b{t}\b", stmt) for t in _SKIP_TABLES):
            continue
        kind = re.match(r"CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX)\b", stmt, re.I)
        if not kind:
            continue
        stmt = re.sub(r"\bSERIAL\s+PRIMARY\s+KEY\b", "INTEGER PRIMARY KEY AUTOINCREMENT", stmt, flags=re.I)
        stmt = re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", stmt, flags=re.I)
        stmt = re.sub(r"\bJSONB\b", "TEXT", stmt, flags=re.I)
        stmt = re.sub(r"\b\w+\[\]", "TEXT", stmt)
        (tables if kind.group(1).upper() == "TABLE" else indexes).append(stmt)
    return tables + indexes


def _connect() -> sqlite3.Connection:
    if _PATH is None:
        raise RuntimeError("the local database is not configured — call sdtm_builder.studio.configure()")
    conn = sqlite3.connect(_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    key = str(_PATH)
    if key not in _READY:
        with _LOCK:
            for stmt in _ddl():
                try:
                    conn.execute(stmt)
                except sqlite3.Error:
                    pass            # a Postgres-only index form: the table still works
            _READY.add(key)
    return conn


def _sql(query: str) -> str:
    q = str(query).replace("NOW()", "CURRENT_TIMESTAMP")
    q = re.sub(r"\s+FOR\s+UPDATE\b", "", q, flags=re.I)
    q = re.sub(r"::\w+(\[\])?", "", q)                   # Postgres casts (::jsonb, ::text)
    return q.replace("%s", "?")


def _param(v):
    return json.dumps(v, default=str) if isinstance(v, (dict, list)) else v


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()] if cur.description else []


class _DB:
    def execute(self, query: str, params: tuple = ()):
        conn = _connect()
        try:
            return _rows(conn.execute(_sql(query), tuple(_param(p) for p in params or ())))
        finally:
            conn.close()

    def transaction(self):
        return _Tx()


class _Tx:
    def __enter__(self):
        self.conn = _connect()
        self.conn.execute("BEGIN IMMEDIATE")
        return self

    def execute(self, query: str, params: tuple = ()):
        return _rows(self.conn.execute(_sql(query), tuple(_param(p) for p in params or ())))

    def __exit__(self, exc_type, exc, tb):
        try:
            self.conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            self.conn.close()
        return False


def get_db() -> _DB:
    return _DB()
