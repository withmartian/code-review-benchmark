"""Tests for db/connection.py: parameter translation, DDL translation, coercion."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from hypothesis import given
from hypothesis import settings
from hypothesis import strategies as st

from db.connection import DBAdapter

# -- _translate_params (SQLite mode) ------------------------------------------


def _sqlite_adapter() -> DBAdapter:
    return DBAdapter("sqlite:///test.db")


def _pg_adapter() -> DBAdapter:
    return DBAdapter("postgresql://localhost/test")


class TestTranslateParamsSqlite:
    def test_simple_placeholders(self) -> None:
        db = _sqlite_adapter()
        sql, args = db._translate_params("SELECT * FROM t WHERE a = $1 AND b = $2", (10, "x"))
        assert sql == "SELECT * FROM t WHERE a = ? AND b = ?"
        assert args == (10, "x")

    def test_reused_placeholder(self) -> None:
        """$2 appearing twice should expand args correctly."""
        db = _sqlite_adapter()
        sql, args = db._translate_params(
            "INSERT INTO t (a, b) VALUES ($1, $2) ON CONFLICT DO UPDATE SET b = $2",
            ("val_a", "val_b"),
        )
        assert sql == "INSERT INTO t (a, b) VALUES (?, ?) ON CONFLICT DO UPDATE SET b = ?"
        assert args == ("val_a", "val_b", "val_b")

    def test_no_placeholders(self) -> None:
        db = _sqlite_adapter()
        sql, args = db._translate_params("SELECT 1", None)
        assert sql == "SELECT 1"
        assert args is None

    def test_out_of_order_placeholders(self) -> None:
        db = _sqlite_adapter()
        _sql, args = db._translate_params("UPDATE t SET a = $2 WHERE id = $1", (1, "new"))
        assert args == ("new", 1)

    def test_empty_args(self) -> None:
        db = _sqlite_adapter()
        sql, args = db._translate_params("SELECT 1", ())
        assert sql == "SELECT 1"
        assert args == ()


class TestTranslateParamsPostgres:
    def test_no_translation(self) -> None:
        """Postgres should pass SQL through without translation."""
        db = _pg_adapter()
        sql, _args = db._translate_params("SELECT * FROM t WHERE a = $1", (10,))
        assert "$1" in sql
        assert "?" not in sql


# -- _coerce_args (Postgres only) ---------------------------------------------


class TestCoerceArgs:
    def test_none_args(self) -> None:
        db = _pg_adapter()
        assert db._coerce_args(None) is None

    def test_iso_timestamp_coerced(self) -> None:
        db = _pg_adapter()
        result = db._coerce_args(("2026-01-15T10:30:00Z",))
        assert isinstance(result[0], datetime)
        assert result[0].year == 2026
        assert result[0].tzinfo is not None

    def test_iso_timestamp_with_offset(self) -> None:
        db = _pg_adapter()
        result = db._coerce_args(("2026-01-15T10:30:00+05:00",))
        assert isinstance(result[0], datetime)

    def test_non_timestamp_string_unchanged(self) -> None:
        db = _pg_adapter()
        result = db._coerce_args(("hello world",))
        assert result[0] == "hello world"

    def test_int_unchanged(self) -> None:
        db = _pg_adapter()
        result = db._coerce_args((42,))
        assert result[0] == 42

    def test_bool_unchanged(self) -> None:
        db = _pg_adapter()
        result = db._coerce_args((True, False))
        assert result[0] is True
        assert result[1] is False

    def test_none_value_unchanged(self) -> None:
        db = _pg_adapter()
        result = db._coerce_args((None,))
        assert result[0] is None

    def test_naive_datetime_gets_utc(self) -> None:
        db = _pg_adapter()
        naive = datetime(2026, 1, 15, 10, 30, 0)
        result = db._coerce_args((naive,))
        assert result[0].tzinfo is not None

    def test_aware_datetime_preserved(self) -> None:
        db = _pg_adapter()
        aware = datetime(2026, 1, 15, 10, 30, 0, tzinfo=UTC)
        result = db._coerce_args((aware,))
        assert result[0] is aware

    def test_mixed_args(self) -> None:
        db = _pg_adapter()
        result = db._coerce_args((42, "2026-01-15T10:30:00Z", "hello", None, True))
        assert result[0] == 42
        assert isinstance(result[1], datetime)
        assert result[2] == "hello"
        assert result[3] is None
        assert result[4] is True


# -- translate_ddl ------------------------------------------------------------


class TestTranslateDdl:
    def test_postgres_passthrough(self) -> None:
        db = _pg_adapter()
        sql = "CREATE TABLE t (id SERIAL PRIMARY KEY, data JSONB)"
        assert db.translate_ddl(sql) == sql

    def test_sqlite_serial(self) -> None:
        db = _sqlite_adapter()
        result = db.translate_ddl("CREATE TABLE t (id SERIAL PRIMARY KEY)")
        assert "INTEGER" in result
        assert "SERIAL" not in result

    def test_sqlite_jsonb(self) -> None:
        db = _sqlite_adapter()
        result = db.translate_ddl("data JSONB")
        assert "TEXT" in result
        assert "JSONB" not in result

    def test_sqlite_timestamptz(self) -> None:
        db = _sqlite_adapter()
        result = db.translate_ddl("created_at TIMESTAMPTZ")
        assert "TEXT" in result
        assert "TIMESTAMPTZ" not in result

    def test_sqlite_boolean_defaults(self) -> None:
        db = _sqlite_adapter()
        result = db.translate_ddl("active INTEGER DEFAULT TRUE")
        assert "DEFAULT 1" in result
        assert "DEFAULT TRUE" not in result

    def test_sqlite_now(self) -> None:
        db = _sqlite_adapter()
        result = db.translate_ddl("created_at TEXT DEFAULT NOW()")
        assert "datetime('now')" in result
        assert "NOW()" not in result


# -- Property-based: _translate_params roundtrip -------------------------------


@given(
    n_params=st.integers(min_value=1, max_value=10),
    values=st.lists(st.one_of(st.integers(), st.text(max_size=20), st.none()), min_size=1, max_size=10),
)
@settings(max_examples=100)
def test_sqlite_translate_preserves_arg_count(n_params: int, values: list) -> None:
    """Number of ? placeholders should equal number of $N refs in output."""
    n = min(n_params, len(values))
    if n == 0:
        return
    placeholders = " AND ".join(f"c{i} = ${i + 1}" for i in range(n))
    sql = f"SELECT * FROM t WHERE {placeholders}"
    args = tuple(values[:n])

    db = _sqlite_adapter()
    translated_sql, translated_args = db._translate_params(sql, args)
    q_count = translated_sql.count("?")
    assert q_count == len(translated_args)
