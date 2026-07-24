"""Pure-Python psycopg-compatible shim backed by pg8000.

psycopg is a C extension with no Android wheel, so it can't ship in the
mobile (.apk) build — flet's Android packaging (serious_python) has no
compiled build for it. pg8000 is pure Python, so it packages fine for
Android. This module implements only the slice of the psycopg API this
codebase actually calls (see database.py / local_pg_manager.py), backed by
pg8000, so those files don't need to change beyond their import line:

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        import psycopg_android_shim as psycopg
        from psycopg_android_shim import dict_row

Desktop always has real psycopg installed, so this module is only ever
loaded on the platform where the real one can't be — this never runs on
desktop and never touches the desktop code path.
"""

import json as _json
import re as _re
from urllib.parse import urlparse as _urlparse, unquote as _unquote

import pg8000.dbapi as _dbapi
from pg8000.exceptions import DatabaseError as _PgDatabaseError

_UNIQUE_VIOLATION_SQLSTATE = "23505"


class Jsonb:
    """Mirrors psycopg.types.json.Jsonb: marks a value for JSONB binding."""

    __slots__ = ("adapted",)

    def __init__(self, value):
        self.adapted = value


class _RowsModule:
    dict_row = object()  # marker object; presence alone means "want dict rows"


rows = _RowsModule()
dict_row = rows.dict_row


class _ErrorsModule:
    class UniqueViolation(Exception):
        pass


errors = _ErrorsModule()


class _JsonModule:
    Jsonb = Jsonb


class types:
    json = _JsonModule()


def _parse_dsn(dsn: str) -> dict:
    """Accepts the postgresql://user:pass@host:port/db[?sslmode=...] DSNs
    this codebase's _build_dsn() always produces."""
    dsn = (dsn or "").strip()
    parsed = _urlparse(dsn)
    query = {}
    if parsed.query:
        for part in parsed.query.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                query[k] = v
    return {
        "user": _unquote(parsed.username or "postgres"),
        "password": _unquote(parsed.password or ""),
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 5432,
        "database": (parsed.path or "/").lstrip("/") or "postgres",
        "sslmode": query.get("sslmode", ""),
    }


def _is_unique_violation(exc: _PgDatabaseError) -> bool:
    try:
        info = exc.args[0]
        return isinstance(info, dict) and info.get("C") == _UNIQUE_VIOLATION_SQLSTATE
    except Exception:
        return False


def _adapt_params(sql: str, params):
    """Rewrites the Nth `%s` to `%s::jsonb` for every Jsonb-wrapped param
    (an explicit cast is more robust than relying on implicit text->jsonb
    coercion, which Postgres does not always apply), and unwraps Jsonb to a
    plain json-serialized string. Plain dict/list params (not wrapped in
    Jsonb) pass through untouched — pg8000 already serializes bare dicts to
    JSON natively; only the explicit-jsonb-column case needs the cast."""
    if not params:
        return sql, params

    jsonb_positions = {i for i, p in enumerate(params) if isinstance(p, Jsonb)}
    if jsonb_positions:
        counter = {"i": 0}

        def _mark(m):
            i = counter["i"]
            counter["i"] += 1
            return m.group(0) + ("::jsonb" if i in jsonb_positions else "")

        sql = _re.sub(r"%s", _mark, sql)

    out_params = tuple(
        _json.dumps(p.adapted) if isinstance(p, Jsonb) else p for p in params
    )
    return sql, out_params


class _Cursor:
    def __init__(self, raw_cursor, want_dict_rows: bool):
        self._cur = raw_cursor
        self._want_dict = want_dict_rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._cur.close()
        return False

    def execute(self, sql, params=None):
        sql2, params2 = _adapt_params(sql, params)
        try:
            self._cur.execute(sql2, params2 or ())
        except _PgDatabaseError as exc:
            if _is_unique_violation(exc):
                raise errors.UniqueViolation(str(exc)) from exc
            raise
        return self

    def _row_to_dict(self, row):
        if row is None or not self._want_dict:
            return row
        cols = [d[0] for d in (self._cur.description or [])]
        return dict(zip(cols, row))

    def fetchone(self):
        return self._row_to_dict(self._cur.fetchone())

    def fetchall(self):
        return [self._row_to_dict(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    def close(self):
        self._cur.close()


class Connection:
    def __init__(self, raw_conn, want_dict_rows: bool):
        self._conn = raw_conn
        self._want_dict = want_dict_rows
        self.closed = False

    def cursor(self):
        return _Cursor(self._conn.cursor(), self._want_dict)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        finally:
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


def connect(
    conninfo: str = "",
    autocommit: bool = False,
    row_factory=None,
    connect_timeout=None,
    **_ignored_kwargs,
) -> Connection:
    info = _parse_dsn(conninfo)
    ssl_ctx = False if info["sslmode"] == "disable" else None  # None = pg8000's opportunistic-TLS default
    raw = _dbapi.Connection(
        user=info["user"],
        password=info["password"],
        host=info["host"],
        port=info["port"],
        database=info["database"],
        timeout=connect_timeout,
        ssl_context=ssl_ctx,
    )
    if autocommit:
        raw.autocommit = True
    return Connection(raw, want_dict_rows=(row_factory is dict_row))
