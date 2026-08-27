"""OPTIONAL live mode: score the REAL model's SQL (needs GROQ_API_KEY).

Same scoring as runner.py, but the SQL comes from GroqProvider instead of the gold
SQL. The gold SQL is now only the REFERENCE we compare the model's rows against.
This is where accuracy < 100% and calibration becomes meaningful.

Free-tier friendly: 1 sample per question + a short pause between questions so you
stay under the tokens-per-minute ceiling.
"""

from __future__ import annotations

import time

from app.config import Settings
from app.pipeline import Pipeline
from app.providers.groq_provider import GroqProvider

from .compare import compare_results
from .dataset import EvalCase
from .runner import CaseResult, EvalSummary
from .sqlite_db import EvalDatabase


def _live_settings(denied_columns: list[str]) -> Settings:
    return Settings(
        llm_provider="groq",
        cache_enabled=False,
        num_samples=1,               # free-tier friendly (raise on a paid tier)
        judge_mode="conditional",
        max_plan_cost=1e9,
        max_plan_rows=1e9,
        denied_columns=denied_columns,
    )  # groq_api_key is read from env/.env by Settings


def run_all_live(cases: list[EvalCase], denied_columns: list[str],
                 pause_seconds: float = 8.0) -> EvalSummary:
    settings = _live_settings(denied_columns)
    provider = GroqProvider(settings)
    db = EvalDatabase()
    try:
        summary = EvalSummary()
        for case in cases:
            pipe = Pipeline(provider, db, settings=settings)
            ans = pipe.answer(case.question)
            got_block = ans.blocked

            if case.should_block:
                r = CaseResult(
                    id=case.id, question=case.question, correct=got_block,
                    expected_block=True, got_block=got_block,
                    reason=(ans.block_reason or "") if got_block else "should have blocked but answered",
                    gold_sql=case.gold_sql, pipeline_sql=ans.sql, confidence=ans.confidence,
                )
            elif got_block:
                r = CaseResult(
                    id=case.id, question=case.question, correct=False,
                    expected_block=False, got_block=True,
                    reason=f"unexpected block: {ans.block_reason}",
                    gold_sql=case.gold_sql, pipeline_sql=ans.sql, confidence=ans.confidence,
                )
            else:
                gold = db.execute(case.gold_sql)
                match = gold.ok and compare_results(gold.rows, ans.rows,
                                                    order_matters=case.order_matters)
                r = CaseResult(
                    id=case.id, question=case.question, correct=bool(match),
                    expected_block=False, got_block=False,
                    reason="" if match else "rows did not match gold",
                    gold_sql=case.gold_sql, pipeline_sql=ans.sql,
                    confidence=ans.confidence, row_match=bool(match),
                )

            summary.results.append(r)
            summary.total += 1
            summary.correct += 1 if r.correct else 0
            time.sleep(pause_seconds)  # stay under the free-tier TPM ceiling
        return summary
    finally:
        db.close()
