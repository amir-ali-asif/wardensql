"""Schema context (for prompting) and schema-hallucination validation.

build_schema_context() renders the live schema for the model prompt.
validate_references() re-checks generated SQL against that schema and flags any
reference to a table/column that does not exist -- the deterministic half of
hallucination detection.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from .ports import SchemaMap


def build_schema_context(schema: SchemaMap) -> str:
    lines = []
    for table, cols in schema.items():
        cols_text = ", ".join(f"{c} {t}" for c, t in cols.items())
        lines.append(f"{table}({cols_text})")
    return "\n".join(lines)


@dataclass
class ReferenceCheck:
    ok: bool
    reason: str | None = None


def validate_references(sql: str, schema: SchemaMap, *, dialect: str = "postgres") -> ReferenceCheck:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except ParseError as e:
        return ReferenceCheck(ok=False, reason=f"parse error: {e}")
    if tree is None:
        return ReferenceCheck(ok=False, reason="empty statement")
    try:
        qualify(tree, schema=schema, dialect=dialect, validate_qualify_columns=True)
    except OptimizeError as e:
        return ReferenceCheck(ok=False, reason=str(e).splitlines()[0])
    return ReferenceCheck(ok=True)
