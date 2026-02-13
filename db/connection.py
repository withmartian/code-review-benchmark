"""Async database adapter wrapping aiosqlite or asyncpg."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import aiosqlite

# Regex for ISO 8601 timestamps: YYYY-MM-DDTHH:MM:SS with optional fractional seconds and timezone
_ISO_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"          # date
    r"[T ]\d{2}:\d{2}:\d{2}"       # time (T or space separator)
    r"(?:\.\d+)?"                    # optional fractional seconds
    r"(?:Z|[+-]\d{2}:\d{2})?$"      # optional timezone
)




class DBAdapter:
    """Thin async adapter that normalizes SQLite vs Postgres differences.

    Translates:
    - Placeholders: $1, $2 ... <-> ?
    - Types: SERIAL <-> INTEGER PRIMARY KEY AUTOINCREMENT,
             JSONB <-> TEXT, NOW() <-> datetime('now')
    - BOOLEAN: true/false literals for Postgres, 1/0 for SQLite
    """

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.is_postgres = database_url.startswith("postgresql")
        self._pg_pool = None  # asyncpg connection pool
        self._sqlite_conn: aiosqlite.Connection | None = None
        self._in_transaction = False

    # -- Connection lifecycle --------------------------------------------------

    async def connect(self) -> None:
        if self.is_postgres:
            import asyncpg
            self._pg_pool = await asyncpg.create_pool(self.database_url, min_size=2, max_size=20)
        else:
            db_path = self.database_url.replace("sqlite:///", "")
            self._sqlite_conn = await aiosqlite.connect(db_path)
            self._sqlite_conn.row_factory = aiosqlite.Row
            await self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
            await self._sqlite_conn.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self.is_postgres and self._pg_pool:
            await self._pg_pool.close()
        elif self._sqlite_conn:
            await self._sqlite_conn.close()

    def transaction(self):
        """Return an async context manager that wraps operations in a transaction.

        Usage:
            async with db.transaction():
                await db.execute(...)
                await db.execute(...)
        """
        return _Transaction(self)

    # -- SQL translation -------------------------------------------------------

    def translate_ddl(self, sql: str) -> str:
        """Translate Postgres-native DDL to SQLite-compatible DDL."""
        if self.is_postgres:
            return sql
        s = sql
        s = re.sub(r'SERIAL\b', 'INTEGER', s)
        s = s.replace('JSONB', 'TEXT')
        s = s.replace('NOW()', "datetime('now')")
        s = s.replace('BOOLEAN', 'INTEGER')
        s = s.replace('TIMESTAMPTZ', 'TEXT')
        s = s.replace('TIMESTAMP', 'TEXT')
        s = s.replace('CURRENT_TEXT', "datetime('now')")
        # Remove Postgres-specific DEFAULT true/false for boolean columns
        s = s.replace('DEFAULT TRUE', 'DEFAULT 1')
        s = s.replace('DEFAULT FALSE', 'DEFAULT 0')
        return s

    def _coerce_args(self, args: tuple | list | None) -> tuple | None:
        """Coerce argument types for asyncpg (Postgres only).

        - ISO 8601 timestamp strings -> datetime objects (with UTC if naive)
        - int 0/1 used as booleans -> proper Python bool (heuristic: skip)
          Note: We can't reliably distinguish int-as-bool from int-as-int,
          so booleans should be passed as True/False from callers. This method
          focuses on timestamp coercion which is the main pain point.
        - datetime objects pass through (naive ones get UTC attached)
        """
        if args is None:
            return None
        coerced = []
        for val in args:
            if isinstance(val, str) and _ISO_TS_RE.match(val):
                # Convert ISO string to datetime
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                coerced.append(dt)
            elif isinstance(val, datetime):
                # Ensure timezone-aware
                if val.tzinfo is None:
                    val = val.replace(tzinfo=timezone.utc)
                coerced.append(val)
            else:
                coerced.append(val)
        return tuple(coerced)

    def _translate_params(self, sql: str, args: tuple | list | None) -> tuple[str, tuple | list | None]:
        """Translate $N placeholders to ? for SQLite.

        Handles re-used parameters (e.g. $2 appearing twice in ON CONFLICT)
        by expanding the args list so each ? gets the right positional value.
        """
        if self.is_postgres:
            return sql, self._coerce_args(args)
        if args is None:
            return sql, args
        # Find all $N references in order of appearance
        refs = re.findall(r'\$(\d+)', sql)
        if not refs:
            return sql, args
        # Build expanded args list matching each ? to the referenced $N
        args_tuple = tuple(args)
        expanded = tuple(args_tuple[int(r) - 1] for r in refs)
        translated = re.sub(r'\$\d+', '?', sql)
        return translated, expanded

    # -- Query execution -------------------------------------------------------

    async def execute(self, sql: str, args: tuple | list | None = None) -> None:
        """Execute a statement (INSERT, UPDATE, CREATE, etc.)."""
        sql, args = self._translate_params(sql, args)
        if self.is_postgres:
            async with self._pg_pool.acquire() as conn:
                await conn.execute(sql, *(args or ()))
        else:
            await self._sqlite_conn.execute(sql, args or ())
            if not self._in_transaction:
                await self._sqlite_conn.commit()

    async def execute_many(self, sql: str, args_list: list[tuple | list]) -> None:
        """Execute a statement for each set of args."""
        if self.is_postgres:
            coerced_list = [self._coerce_args(a) for a in args_list]
            async with self._pg_pool.acquire() as conn:
                await conn.executemany(sql, coerced_list)
        else:
            sql_translated, _ = self._translate_params(sql, () if not args_list else args_list[0])
            await self._sqlite_conn.executemany(sql_translated, args_list)
            await self._sqlite_conn.commit()

    async def fetchone(self, sql: str, args: tuple | list | None = None) -> dict[str, Any] | None:
        """Fetch a single row as a dict."""
        sql, args = self._translate_params(sql, args)
        if self.is_postgres:
            async with self._pg_pool.acquire() as conn:
                row = await conn.fetchrow(sql, *(args or ()))
            return dict(row) if row else None
        else:
            cursor = await self._sqlite_conn.execute(sql, args or ())
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    async def fetchall(self, sql: str, args: tuple | list | None = None) -> list[dict[str, Any]]:
        """Fetch all rows as a list of dicts."""
        sql, args = self._translate_params(sql, args)
        if self.is_postgres:
            async with self._pg_pool.acquire() as conn:
                rows = await conn.fetch(sql, *(args or ()))
            return [dict(r) for r in rows]
        else:
            cursor = await self._sqlite_conn.execute(sql, args or ())
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def fetchval(self, sql: str, args: tuple | list | None = None) -> Any:
        """Fetch a single scalar value."""
        sql, args = self._translate_params(sql, args)
        if self.is_postgres:
            async with self._pg_pool.acquire() as conn:
                return await conn.fetchval(sql, *(args or ()))
        else:
            cursor = await self._sqlite_conn.execute(sql, args or ())
            row = await cursor.fetchone()
            if row is None:
                return None
            return row[0]


class _Transaction:
    """Async context manager for wrapping operations in a DB transaction.

    For Postgres, acquires a dedicated connection from the pool and temporarily
    patches the adapter so all calls within the block use that connection.
    """

    def __init__(self, db: DBAdapter):
        self._db = db
        self._pg_tr = None
        self._pg_conn = None

    async def __aenter__(self):
        self._db._in_transaction = True
        if self._db.is_postgres:
            self._pg_conn = await self._db._pg_pool.acquire()
            self._pg_tr = self._pg_conn.transaction()
            await self._pg_tr.start()
            # Temporarily swap pool for a single-conn wrapper so execute/fetch
            # calls within this transaction use this connection
            self._saved_pool = self._db._pg_pool
            self._db._pg_pool = _SingleConnPool(self._pg_conn)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._db._in_transaction = False
        if self._db.is_postgres and self._pg_tr:
            if exc_type is not None:
                await self._pg_tr.rollback()
            else:
                await self._pg_tr.commit()
            self._db._pg_pool = self._saved_pool
            await self._saved_pool.release(self._pg_conn)
        elif self._db._sqlite_conn:
            if exc_type is not None:
                await self._db._sqlite_conn.rollback()
            else:
                await self._db._sqlite_conn.commit()


class _SingleConnPool:
    """Minimal shim that makes a single connection look like a pool for transaction blocks."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _SingleConnCtx(self._conn)


class _SingleConnCtx:
    """Context manager that returns the same connection without releasing it."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        pass
