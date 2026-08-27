"""Load the evaluation dataset: questions paired with known-correct SQL."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EvalCase:
    id: str
    question: str
    gold_sql: str | None = None      # the hand-written correct query (None if should_block)
    should_block: bool = False       # True => the right behavior is to refuse
    order_matters: bool = False      # True => row order is part of correctness
    notes: str = ""


def load_cases(path: str | Path) -> list[EvalCase]:
    """Read a .jsonl file into a list of EvalCase, validating as we go."""
    path = Path(path)
    cases: list[EvalCase] = []

    with path.open(encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue  # allow blank lines and # comments
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {e}") from e

            if "id" not in obj or "question" not in obj:
                raise ValueError(f"{path}:{line_no}: every case needs 'id' and 'question'")

            cases.append(
                EvalCase(
                    id=str(obj["id"]),
                    question=str(obj["question"]),
                    gold_sql=obj.get("gold_sql"),
                    should_block=bool(obj.get("should_block", False)),
                    order_matters=bool(obj.get("order_matters", False)),
                    notes=str(obj.get("notes", "")),
                )
            )

    if not cases:
        raise ValueError(f"{path}: no cases found")

    for c in cases:
        if not c.should_block and not c.gold_sql:
            raise ValueError(f"case '{c.id}': needs 'gold_sql' unless should_block is true")

    return cases
