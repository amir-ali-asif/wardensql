"""Read-only Postgres access with connection pooling and schema caching.

- A connection pool (not a new connection per request) is what lets the service
  handle concurrency without exhausting Postgres.
- Schema is introspected once and cached with a TTL, not re-read on every question.
- explain_cost() runs EXPLAIN (read-only) to estimate cost before execution.
- execute() runs inside a READ ONLY transaction with a statement timeout.

psycopg is imported lazily so the test suite can run without it installed.
"""

from __future__ import annotations

import time

from .ports import Database, ExecutionResult, ExplainCost, SchemaMap
from .explain import parse_explain_cost

_INTROSPECT_SQL = """
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position
"""


class PostgresDatabase(Database):
    def __init__(self, settings) -> None:
        from psycopg_pool import ConnectionPool  # lazy import
        self._settings = settings
        self._pool = ConnectionPool(
            settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            open=True,
            kwargs={"autocommit": True},
        )
        self._schema_cache: SchemaMap | None = None
        self._schema_cached_at = 0.0

    def close(self) -> None:
        self._pool.close()

    def ping(self) -> bool:
        try:
            with self._pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def load_schema(self) -> SchemaMap:
        now = time.monotonic()
        if (self._schema_cache is not None
                and now - self._schema_cached_at < self._settings.schema_cache_ttl_seconds):
            return self._schema_cache

        schema: SchemaMap = {}
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(_INTROSPECT_SQL)
                for table, column, dtype in cur.fetchall():
                    schema.setdefault(table, {})[column] = dtype
        self._schema_cache = schema
        self._schema_cached_at = now
        return schema

    def explain_cost(self, sql: str) -> ExplainCost:
        import psycopg
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(f"SET LOCAL statement_timeout = {self._settings.statement_timeout_ms}")
                        cur.execute("SET TRANSACTION READ ONLY")
                        cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
                        row = cur.fetchone()
            return parse_explain_cost(row[0])
        except psycopg.Error as e:
            return ExplainCost(ok=False, error=str(e).strip())

    def execute(self, sql: str) -> ExecutionResult:
        import psycopg
        cap = self._settings.max_rows
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute(f"SET LOCAL statement_timeout = {self._settings.statement_timeout_ms}")
                        cur.execute("SET TRANSACTION READ ONLY")
                        cur.execute(sql)
                        columns = [d.name for d in cur.description] if cur.description else []
                        rows = [list(r) for r in cur.fetchmany(cap + 1)]
            truncated = len(rows) > cap
            return ExecutionResult(
                ok=True, columns=columns, rows=rows[:cap],
                row_count=min(len(rows), cap), truncated=truncated,
            )
        except psycopg.Error as e:
            return ExecutionResult(ok=False, error=str(e).strip())
