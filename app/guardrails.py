"""Static SQL guardrails (application-level half of defense-in-depth).

Parses SQL to an AST and permits only a single read-only SELECT/CTE/set-operation,
rejecting writes, DDL, stacked statements, SELECT INTO, raw commands, and dangerous
functions. Also caps the row count with a LIMIT.

This is NOT the last line of defense -- the read-only Postgres role (sql/setup.sql)
is. If a query slips past this parser, the database itself must still refuse to write.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Drop, exp.Create,
    exp.Alter, exp.TruncateTable, exp.Grant, exp.Into, exp.Command,
)

_ALLOWED_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select, exp.Union, exp.Intersect, exp.Except,
)

_FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset({
    "pg_sleep", "pg_read_file", "pg_read_binary_file", "lo_import", "lo_export",
    "dblink", "pg_terminate_backend", "pg_cancel_backend",
})


@dataclass
class GuardrailResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    safe_sql: str | None = None


def check_sql(sql: str, *, dialect: str = "postgres", max_rows: int = 1000) -> GuardrailResult:
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except ParseError as e:
        return GuardrailResult(ok=False, reasons=[f"could not parse SQL: {e}"])

    if not statements:
        return GuardrailResult(ok=False, reasons=["no statement found"])
    if len(statements) > 1:
        return GuardrailResult(ok=False, reasons=["multiple statements are not allowed"])

    tree = statements[0]
    reasons: list[str] = []

    if not isinstance(tree, _ALLOWED_ROOTS):
        reasons.append(f"only SELECT queries are allowed, got {type(tree).__name__}")

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            reasons.append(f"forbidden operation: {type(node).__name__}")

    for fn in tree.find_all(exp.Anonymous):
        if (fn.name or "").lower() in _FORBIDDEN_FUNCTIONS:
            reasons.append(f"forbidden function: {fn.name.lower()}")

    if reasons:
        return GuardrailResult(ok=False, reasons=sorted(set(reasons)))

    safe = _enforce_limit(tree, max_rows)
    return GuardrailResult(ok=True, safe_sql=safe.sql(dialect=dialect))


def _enforce_limit(tree: exp.Expression, max_rows: int) -> exp.Expression:
    existing = tree.args.get("limit")
    if existing is not None:
        try:
            if int(existing.expression.name) <= max_rows:
                return tree
        except (AttributeError, ValueError):
            pass
    return tree.limit(max_rows)
