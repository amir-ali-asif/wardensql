"""Confidence scoring from self-consistency (not model self-report).

Sample k queries, canonicalize so cosmetic differences collapse, and measure
agreement. Combine that with the judge score and execution success, gated by the
hard checks (guardrails, schema, policy) which can zero the score out entirely.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import sqlglot
from sqlglot.errors import ParseError


def canonicalize(sql: str, *, dialect: str = "postgres") -> str | None:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except ParseError:
        return None
    if tree is None:
        return None
    return tree.sql(dialect=dialect, normalize=True, comments=False)


def consistency(candidates: list[str], *, dialect: str = "postgres") -> tuple[str, float]:
    if not candidates:
        return "", 0.0
    canon_to_original: dict[str, str] = {}
    counts: Counter[str] = Counter()
    for sql in candidates:
        c = canonicalize(sql, dialect=dialect)
        if c is None:
            continue
        counts[c] += 1
        canon_to_original.setdefault(c, sql)
    if not counts:
        return candidates[0], 0.0
    best_canon, best_n = counts.most_common(1)[0]
    return canon_to_original[best_canon], best_n / len(candidates)


@dataclass
class Confidence:
    score: float
    blocked: bool
    signals: dict = field(default_factory=dict)


def score(
    *,
    guardrail_ok: bool,
    policy_ok: bool,
    schema_ok: bool,
    execution_ok: bool,
    consistency_fraction: float,
    judge_score: float,
) -> Confidence:
    signals = {
        "guardrail_ok": guardrail_ok,
        "policy_ok": policy_ok,
        "schema_ok": schema_ok,
        "execution_ok": execution_ok,
        "consistency": round(consistency_fraction, 3),
        "judge_score": round(judge_score, 3),
    }
    if not (guardrail_ok and policy_ok and schema_ok):
        return Confidence(score=0.0, blocked=True, signals=signals)

    graded = (
        0.50 * consistency_fraction
        + 0.35 * judge_score
        + 0.15 * (1.0 if execution_ok else 0.0)
    )
    return Confidence(score=round(graded, 3), blocked=False, signals=signals)
