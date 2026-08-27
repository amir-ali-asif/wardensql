"""CLI: ask a question straight from the terminal.

    python -m app.cli "how many completed orders per country?"
"""

from __future__ import annotations

import sys

from .config import settings
from .db import PostgresDatabase
from .observability import configure_logging
from .pipeline import Pipeline
from .policy import Policy
from .providers import get_provider


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python -m app.cli "your question"')
        raise SystemExit(1)

    configure_logging()
    db = PostgresDatabase(settings)
    policy = Policy(
        allowed_tables=settings.allowed_tables,
        denied_tables=settings.denied_tables,
        denied_columns=settings.denied_columns,
        table_aliases=settings.table_aliases,
    )
    pipe = Pipeline(get_provider(settings), db, settings=settings, policy=policy)
    try:
        ans = pipe.answer(" ".join(sys.argv[1:]))
    finally:
        db.close()

    print(f"\nQ: {ans.question}")
    if ans.blocked:
        print(f"BLOCKED: {ans.block_reason}")
        print(f"signals: {ans.signals}")
        return
    print(f"\nSQL:\n{ans.sql}")
    print(f"\nconfidence: {ans.confidence}   {ans.signals}")
    if ans.judge_reason:
        print(f"judge: {ans.judge_reason}")
    print(f"cached={ans.cached}  tokens={ans.tokens_used}  latency={ans.latency_ms}ms")
    print(f"\ncolumns: {ans.columns}")
    for row in ans.rows[:20]:
        print("  ", row)
    if ans.truncated:
        print("   ... (results truncated)")


if __name__ == "__main__":
    main()
