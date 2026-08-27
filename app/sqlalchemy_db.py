"""Stage 1: connect to ANY database through a single interface.

This is the multi-engine sibling of PostgresDatabase. Where PostgresDatabase speaks
only to Postgres via psycopg, this class speaks to Postgres, MySQL/MariaDB, SQLite,
SQL Server, and anything else SQLAlchemy has a driver for -- all behind the exact
same app.ports.Database interface (load_schema / explain_cost / execute), so the
pipeline accepts it with zero changes.

Why this exists
---------------
The original service was wired to one hardcoded Postgres database. To turn the
project into a tool a company can point at *their* database, we needed a backend
that (a) takes a connection string, (b) auto-discovers the schema, and (c) still
honours the project's safety rules. SQLAlchemy's dialect + reflection machinery
does the heavy lifting; this class adapts it to our ports and re-imposes our own
guarantees on top.

Safety notes (important, and honestly scoped)
--------------------------------------------
* Read-only is enforced in THREE independent ways, exactly as before: the AST
  guardrails + resolver refuse writes/DDL before any SQL reaches here, execute()
  runs inside a transaction that is rolled back (never committed), and users are
  urged (in the UI + README) to connect with a read-only DB user. The strongest of
  these remains a read-only account -- connect with least privilege.
* explain_cost() is best-effort and dialect-aware. Postgres gets a real costed
  plan (same as PostgresDatabase); other engines that lack a comparable costed
  EXPLAIN return ok=True with cost 0 so the cost-guard stage is a no-op rather than
  a false block. The row cap and statement handling still apply.
* A per-connection dialect string is derived so the pipeline's sqlglot layers parse
  the right flavor of SQL.
"""

from __future__ import annotations

import json
import time

from .explain import parse_explain_cost
from .ports import Database, ExecutionResult, ExplainCost, SchemaMap

# Map SQLAlchemy dialect names -> the dialect string sqlglot expects. Anything not
# listed falls back to "postgres", a safe, widely-compatible default for the AST
# safety layers.
_SQLGLOT_DIALECT = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlite": "sqlite",
    "mssql": "tsql",
    "oracle": "oracle",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "redshift": "redshift",
}


def sqlglot_dialect_for(sqlalchemy_dialect: str) -> str:
    return _SQLGLOT_DIALECT.get(sqlalchemy_dialect.lower(), "postgres")


class SqlAlchemyDatabase(Database):
    """A read-only, introspecting Database backed by any SQLAlchemy engine."""

    def __init__(self, url: str, *, settings=None, connect_timeout: int = 10) -> None:
        from sqlalchemy import create_engine  # lazy import (optional dependency)

        self._settings = settings
        self._url = url
        # pool_pre_ping avoids handing out dead connections; future=True uses the
        # modern 2.0-style API. We keep the pool small and lazy.
        self._engine = create_engine(
            url,
            pool_pre_ping=True,
            future=True,
            pool_size=getattr(settings, "pool_max_size", 5) if settings else 5,
        )
        self.sqlalchemy_dialect = self._engine.dialect.name
        self.dialect = sqlglot_dialect_for(self.sqlalchemy_dialect)

        self._schema_cache: SchemaMap | None = None
        self._schema_cached_at = 0.0

    # ---- lifecycle -------------------------------------------------------

    def close(self) -> None:
        self._engine.dispose()

    def ping(self) -> bool:
        from sqlalchemy import text
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # ---- introspection ---------------------------------------------------

    def load_schema(self) -> SchemaMap:
        """Auto-discover tables + columns via SQLAlchemy reflection.

        This is the magic that makes "connect any database and start asking" work:
        the moment we connect, we read the live schema, so the pipeline knows the
        real tables/columns (and the governance + hallucination layers have truth
        to check against)."""
        ttl = getattr(self._settings, "schema_cache_ttl_seconds", 300) if self._settings else 300
        now = time.monotonic()
        if self._schema_cache is not None and now - self._schema_cached_at < ttl:
            return self._schema_cache

        from sqlalchemy import inspect

        schema: SchemaMap = {}
        inspector = inspect(self._engine)
        # Reflect the default schema only (keeps things simple + safe); companies
        # with multiple schemas can extend this later.
        for table in inspector.get_table_names():
            cols: dict[str, str] = {}
            for col in inspector.get_columns(table):
                cols[col["name"]] = str(col.get("type", "")).upper() or "TEXT"
            schema[table] = cols
        # Views are queryable too -- include them so questions over views work.
        try:
            for view in inspector.get_view_names():
                cols = {}
                for col in inspector.get_columns(view):
                    cols[col["name"]] = str(col.get("type", "")).upper() or "TEXT"
                schema.setdefault(view, cols)
        except Exception:
            pass  # some dialects don't support view reflection

        self._schema_cache = schema
        self._schema_cached_at = now
        return schema

    def table_count(self) -> int:
        return len(self.load_schema())

    # ---- cost guard ------------------------------------------------------

    def explain_cost(self, sql: str) -> ExplainCost:
        """Best-effort, dialect-aware cost estimate.

        Postgres: a real costed JSON plan (same signal as PostgresDatabase).
        Others: we don't have a portable costed EXPLAIN, so we return ok with zero
        cost -- the cost guard becomes a no-op rather than a false positive. The
        row cap in execute() still bounds result size."""
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        if self.sqlalchemy_dialect in ("postgresql", "postgres"):
            try:
                with self._engine.connect() as conn:
                    self._apply_readonly(conn)
                    row = conn.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")).fetchone()
                # psycopg returns already-parsed JSON; other drivers may hand back a
                # JSON string -- handle both.
                plan = row[0]
                if isinstance(plan, str):
                    plan = json.loads(plan)
                return parse_explain_cost(plan)
            except SQLAlchemyError as e:
                return ExplainCost(ok=False, error=str(e).strip())
            except Exception:
                return ExplainCost(ok=True, total_cost=0.0, plan_rows=0.0)

        # Non-Postgres: skip costing, don't block.
        return ExplainCost(ok=True, total_cost=0.0, plan_rows=0.0)

    # ---- execution -------------------------------------------------------

    def _apply_readonly(self, conn) -> None:
        """Set a statement timeout + read-only mode where the dialect supports it.
        Any failure here is non-fatal: the AST guardrails already guarantee the
        statement is a read-only SELECT."""
        from sqlalchemy import text

        timeout = getattr(self._settings, "statement_timeout_ms", 5000) if self._settings else 5000
        d = self.sqlalchemy_dialect
        try:
            if d in ("postgresql", "postgres"):
                conn.execute(text(f"SET statement_timeout = {int(timeout)}"))
                conn.execute(text("SET TRANSACTION READ ONLY"))
            elif d in ("mysql", "mariadb"):
                conn.execute(text(f"SET SESSION max_execution_time = {int(timeout)}"))
        except Exception:
            pass

    def execute(self, sql: str) -> ExecutionResult:
        """Run the (already-safety-checked) SELECT inside a transaction that is
        ALWAYS rolled back -- so even if a write somehow reached here, it could not
        persist. Returns at most max_rows(+1 to detect truncation) rows."""
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        cap = getattr(self._settings, "max_rows", 1000) if self._settings else 1000
        try:
            with self._engine.connect() as conn:
                trans = conn.begin()
                try:
                    self._apply_readonly(conn)
                    result = conn.execute(text(sql))
                    columns = list(result.keys())
                    fetched = result.fetchmany(cap + 1)
                    rows = [list(r) for r in fetched]
                finally:
                    trans.rollback()  # never commit: read-only by construction
            truncated = len(rows) > cap
            return ExecutionResult(
                ok=True, columns=columns, rows=rows[:cap],
                row_count=min(len(rows), cap), truncated=truncated,
            )
        except SQLAlchemyError as e:
            return ExecutionResult(ok=False, error=str(e).strip())
