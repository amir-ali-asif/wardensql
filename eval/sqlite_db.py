"""An in-memory SQLite database that satisfies app.ports.Database.

Purpose: let the eval harness EXECUTE real SQL (gold and predicted) fully offline
-- no Postgres server, no network. SQLite ships with Python (sqlite3), so anyone
who clones the repo can run the eval immediately.

It implements the same three methods the pipeline needs (load_schema, explain_cost,
execute) and returns the same dataclasses from app.ports, so the pipeline accepts it
in place of PostgresDatabase with zero changes to app/.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.ports import ExecutionResult, ExplainCost, SchemaMap

_SEED = Path(__file__).parent / "seed_sqlite.sql"

_TYPE = {"INTEGER": "INT", "REAL": "NUMERIC", "TEXT": "TEXT", "": "TEXT"}


class EvalDatabase:
    def __init__(self) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.executescript(_SEED.read_text(encoding="utf-8"))

    def close(self) -> None:
        self._conn.close()

    def load_schema(self) -> SchemaMap:
        schema: SchemaMap = {}
        cur = self._conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            cur.execute(f"PRAGMA table_info({t})")
            cols = {row[1]: _TYPE.get((row[2] or "").upper(), "TEXT") for row in cur.fetchall()}
            schema[t] = cols
        return schema

    def explain_cost(self, sql: str) -> ExplainCost:
        """SQLite has no Postgres-style costed plan. For the OFFLINE eval we return a
        constant, safe, always-under-budget cost so the cost-guard stage passes and
        we can measure the parts that matter (accuracy, calibration)."""
        return ExplainCost(ok=True, total_cost=1.0, plan_rows=1.0)

    def execute(self, sql: str) -> ExecutionResult:
        try:
            cur = self._conn.cursor()
            cur.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchall()]
            return ExecutionResult(
                ok=True, columns=columns, rows=rows, row_count=len(rows),
            )
        except sqlite3.Error as e:
            return ExecutionResult(ok=False, error=str(e).strip())
