"""Run every eval case through the REAL pipeline and score it.

For each case:
  * seed a FakeProvider with the case's gold SQL (so the LLM step is deterministic),
  * run the case's QUESTION through the real Pipeline (all safety gates active),
  * run the gold SQL directly to get the reference rows,
  * compare with the Task-2 oracle,
  * record one CaseResult.

This measures PIPELINE FIDELITY offline: given correct SQL, does the pipeline let
valid queries through and block the ones policy forbids? (Measuring the live model's
SQL quality is a separate, key-required mode in runner_live.py.)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings
from app.pipeline import Pipeline
from app.providers.fake import FakeProvider

from .compare import compare_results
from .dataset import EvalCase
from .sqlite_db import EvalDatabase


@dataclass
class CaseResult:
    id: str
    question: str
    correct: bool
    expected_block: bool
    got_block: bool
    reason: str = ""
    gold_sql: str | None = None
    pipeline_sql: str | None = None
    confidence: float = 0.0
    row_match: bool | None = None


def _eval_settings(denied_columns: list[str]) -> Settings:
    return Settings(
        llm_provider="fake",
        groq_api_key="x",
        cache_enabled=False,
        num_samples=1,
        judge_mode="off",
        max_plan_cost=1e9,
        max_plan_rows=1e9,
        denied_columns=denied_columns,
    )


def run_case(case: EvalCase, db: EvalDatabase, denied_columns: list[str]) -> CaseResult:
    settings = _eval_settings(denied_columns)

    candidate = case.gold_sql or "SELECT 1"
    provider = FakeProvider(candidates=[candidate])
    pipe = Pipeline(provider, db, settings=settings)

    ans = pipe.answer(case.question)
    got_block = ans.blocked

    if case.should_block:
        correct = got_block
        return CaseResult(
            id=case.id, question=case.question, correct=correct,
            expected_block=True, got_block=got_block,
            reason=(ans.block_reason or "") if got_block else "should have blocked but answered",
            gold_sql=case.gold_sql, pipeline_sql=ans.sql, confidence=ans.confidence,
        )

    if got_block:
        return CaseResult(
            id=case.id, question=case.question, correct=False,
            expected_block=False, got_block=True,
            reason=f"unexpected block: {ans.block_reason}",
            gold_sql=case.gold_sql, pipeline_sql=ans.sql, confidence=ans.confidence,
        )

    gold_exec = db.execute(case.gold_sql)
    if not gold_exec.ok:
        return CaseResult(
            id=case.id, question=case.question, correct=False,
            expected_block=False, got_block=False,
            reason=f"gold SQL failed to run: {gold_exec.error} (fix the dataset)",
            gold_sql=case.gold_sql, pipeline_sql=ans.sql, confidence=ans.confidence,
        )

    match = compare_results(gold_exec.rows, ans.rows, order_matters=case.order_matters)
    return CaseResult(
        id=case.id, question=case.question, correct=match,
        expected_block=False, got_block=False,
        reason="" if match else "rows did not match gold",
        gold_sql=case.gold_sql, pipeline_sql=ans.sql, confidence=ans.confidence,
        row_match=match,
    )


@dataclass
class EvalSummary:
    total: int = 0
    correct: int = 0
    results: list[CaseResult] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def run_all(cases: list[EvalCase], denied_columns: list[str]) -> EvalSummary:
    db = EvalDatabase()
    try:
        summary = EvalSummary()
        for case in cases:
            r = run_case(case, db, denied_columns)
            summary.results.append(r)
            summary.total += 1
            summary.correct += 1 if r.correct else 0
        return summary
    finally:
        db.close()
