"""Core types and interfaces (ports).

Everything downstream depends on these Protocols, not on concrete Groq/psycopg
classes. That is what makes the pipeline swappable (Groq -> Gemini -> anything) and
fully testable with fakes -- no live database or API key required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# {table_name: {column_name: type}}
SchemaMap = dict[str, dict[str, str]]


@dataclass
class JudgeVerdict:
    answers_question: bool
    score: float
    reason: str


@dataclass
class Usage:
    """Token + rate-limit accounting surfaced from the LLM provider."""
    total_tokens: int = 0
    calls: int = 0
    remaining_requests: int | None = None
    remaining_tokens: int | None = None


@dataclass
class ExecutionResult:
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    error: str | None = None


@dataclass
class ExplainCost:
    ok: bool
    total_cost: float = 0.0
    plan_rows: float = 0.0
    error: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """A source of SQL candidates and semantic judgements."""

    def generate(self, question: str, schema_context: str, k: int) -> list[str]: ...

    def judge(self, question: str, sql: str, schema_context: str) -> JudgeVerdict: ...

    def usage(self) -> Usage: ...


@runtime_checkable
class Database(Protocol):
    """A read-only data source that can introspect, cost-estimate, and execute."""

    def load_schema(self) -> SchemaMap: ...

    def explain_cost(self, sql: str) -> ExplainCost: ...

    def execute(self, sql: str) -> ExecutionResult: ...
