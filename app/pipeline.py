"""End-to-end orchestration.

Stages (each a separate, testable component behind an interface):
  0. cache lookup            -> return instantly, zero LLM calls, on a repeat question
  1. generate k candidates   -> parallel sampling
  2. self-consistency        -> chosen query + agreement fraction
  3. guardrails              -> static AST safety (hard gate)
  4. governance policy       -> allow/deny tables & columns (hard gate)
  5. schema validation       -> real identifiers only (hard gate)
  6. EXPLAIN cost guard       -> reject plans that would scan too much
  7. execute                 -> read-only, timeout-bounded
  8. judge (conditional)     -> semantic critic, only when consistency is ambiguous
  9. score + audit + cache   -> confidence, audit event, cache the result

The pipeline receives its provider, database, policy and cache by injection, so it
runs unchanged against real Groq+Postgres or against fakes in tests.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable

from . import confidence, guardrails, schema as schema_mod
from .cache import TTLCache, make_key
from .explain import within_budget
from .observability import audit, metrics
from .policy import Policy
from .ports import Database, ExecutionResult, LLMProvider

logger = logging.getLogger("text2sql.pipeline")


@dataclass
class Answer:
    question: str
    sql: str | None
    blocked: bool
    block_reason: str | None
    confidence: float
    signals: dict = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    judge_reason: str | None = None
    cached: bool = False
    tokens_used: int = 0
    latency_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Canonical, ordered list of pipeline stages, shared with the UI so it can render a
# live "processing" timeline and mark each stage as it resolves. (step_id, label)
PIPELINE_STAGES: list[tuple[str, str]] = [
    ("cache", "Cache lookup"),
    ("generate", "Generate SQL candidates"),
    ("consistency", "Self-consistency vote"),
    ("guardrails", "Guardrails (AST safety)"),
    ("policy", "Governance policy (ABAC)"),
    ("schema", "Schema validation"),
    ("explain", "Cost guard (EXPLAIN)"),
    ("execute", "Execute (read-only)"),
    ("judge", "LLM judge"),
    ("score", "Confidence & result"),
]


@dataclass
class StepEvent:
    """One pipeline stage resolving, streamed to observers (e.g. the web UI)."""
    step: str                              # stage id, one of PIPELINE_STAGES
    label: str                             # human label
    status: str                            # "ok" | "blocked" | "skip" | "info"
    detail: str = ""                       # short human explanation
    data: dict = field(default_factory=dict)   # optional structured payload

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# A sink for step events. Kept synchronous + simple so any transport (SSE, logging,
# a test list) can consume it without the pipeline knowing about the transport.
StepCallback = Callable[[StepEvent], None]


class Pipeline:
    def __init__(
        self,
        provider: LLMProvider,
        database: Database,
        *,
        settings,
        policy: Policy | None = None,
        cache: TTLCache | None = None,
    ) -> None:
        self.provider = provider
        self.db = database
        self.settings = settings
        # Build the governance policy from settings when one isn't injected, so
        # configured deny/allow rules are never silently dropped.
        self.policy = policy or Policy(
            allowed_tables=settings.allowed_tables,
            denied_tables=settings.denied_tables,
            denied_columns=settings.denied_columns,
            table_aliases=settings.table_aliases,
        )
        self.cache = cache if cache is not None else (
            TTLCache(settings.cache_ttl_seconds) if settings.cache_enabled else None
        )

    def answer(self, question: str, *, request_id: str = "-",
               on_step: StepCallback | None = None) -> Answer:
        started = time.monotonic()
        metrics.inc("t2s_requests_total")

        labels = dict(PIPELINE_STAGES)

        def emit(step: str, status: str, detail: str = "", data: dict | None = None) -> None:
            if on_step is not None:
                on_step(StepEvent(step=step, label=labels.get(step, step),
                                  status=status, detail=detail, data=data or {}))

        question = (question or "").strip()
        if not question:
            emit("cache", "blocked", "empty question")
            return self._blocked(question, None, "empty question", {}, started)
        if len(question) > self.settings.max_question_chars:
            emit("cache", "blocked", "question too long")
            return self._blocked(question, None, "question too long", {}, started)

        schema = self.db.load_schema()
        schema_context = schema_mod.build_schema_context(schema)
        cache_key = make_key(question.lower(), schema_context)

        if self.cache is not None:
            hit = self.cache.get(cache_key)
            if hit is not None:
                metrics.inc("t2s_cache_hits_total")
                emit("cache", "ok", "hit — returned cached result, zero LLM calls")
                cached = Answer(**{**hit, "cached": True})
                cached.latency_ms = int((time.monotonic() - started) * 1000)
                return cached
        emit("cache", "skip",
             "miss — running full pipeline" if self.cache is not None else "cache disabled")

        # 1-2. generate + self-consistency
        candidates = self.provider.generate(question, schema_context, self.settings.num_samples)
        emit("generate", "ok", f"{len(candidates)} candidate(s) sampled",
             {"candidates": candidates})
        chosen, agreement = confidence.consistency(candidates)
        emit("consistency", "ok", f"{agreement:.0%} of candidates agreed on the chosen query",
             {"chosen": chosen, "agreement": round(agreement, 3)})

        # 3. guardrails
        dialect = getattr(self.settings, "sql_dialect", "postgres")
        guard = guardrails.check_sql(chosen, dialect=dialect, max_rows=self.settings.max_rows)
        if not guard.ok:
            reason = "; ".join(guard.reasons)
            emit("guardrails", "blocked", reason)
            return self._blocked(question, chosen, "guardrail: " + reason,
                                 {"guardrail_ok": False, "consistency": round(agreement, 3)},
                                 started, request_id=request_id)
        safe_sql = guard.safe_sql
        emit("guardrails", "ok", "read-only SELECT; row cap enforced", {"safe_sql": safe_sql})

        # 4. governance policy (deterministic ABAC with canonical, scope-aware,
        #    fail-closed resolution). Runs BEFORE the judge: no LLM score can rescue
        #    a query that this gate blocks.
        pol = self.policy.check(safe_sql, schema, dialect=dialect)
        if not pol.ok:
            sig = {"policy_ok": False, "consistency": round(agreement, 3),
                   "policy_status": pol.status.value}
            # Record what was written vs. the canonical identity it resolved to, so
            # an auditor can see e.g. "c.ssn -> customers.ssn". Identifiers only --
            # never column values (see §16).
            if pol.original_ref:
                sig["policy_original_ref"] = pol.original_ref
            if pol.canonical_ref:
                sig["policy_canonical_ref"] = pol.canonical_ref
            if pol.candidates:
                sig["policy_ambiguous_candidates"] = pol.candidates
            for key in ("denied_refs", "unresolved_refs", "ambiguous_refs"):
                vals = getattr(pol, key)
                if vals:
                    sig[f"policy_{key}"] = vals
            resolved = (f"{pol.original_ref} → {pol.canonical_ref}"
                        if pol.original_ref and pol.canonical_ref else "")
            emit("policy", "blocked", pol.reason,
                 {"resolved": resolved, "status": pol.status.value,
                  "explanation": pol.explanation})
            return self._blocked(question, safe_sql, f"policy: {pol.reason}", sig,
                                 started, request_id=request_id)
        emit("policy", "ok",
             "every column reference resolved to a permitted canonical column",
             {"status": pol.status.value, "resolved_refs": pol.resolved_refs})

        # 5. schema-hallucination check
        ref = schema_mod.validate_references(safe_sql, schema, dialect=dialect)
        if not ref.ok:
            emit("schema", "blocked", ref.reason)
            return self._blocked(question, safe_sql, f"schema hallucination: {ref.reason}",
                                 {"schema_ok": False, "consistency": round(agreement, 3)},
                                 started, request_id=request_id)
        emit("schema", "ok", "every table/column exists in the live schema")

        # 6. EXPLAIN cost guard
        cost = self.db.explain_cost(safe_sql)
        budget_ok, budget_reason = within_budget(
            cost, max_cost=self.settings.max_plan_cost, max_rows=self.settings.max_plan_rows)
        if not budget_ok:
            emit("explain", "blocked", budget_reason or "plan over budget")
            return self._blocked(question, safe_sql, f"cost guard: {budget_reason}",
                                 {"guardrail_ok": True, "schema_ok": True, "policy_ok": True,
                                  "consistency": round(agreement, 3)},
                                 started, request_id=request_id)
        emit("explain", "ok",
             f"estimated cost {cost.total_cost:.0f}, rows {cost.plan_rows:.0f}")

        # 7. execute -- then a hard, provider-agnostic result-size cap
        result = self._cap_result(self.db.execute(safe_sql))
        if result.ok:
            emit("execute", "ok",
                 f"{result.row_count} row(s)" + (" (result capped)" if result.truncated else ""))
        else:
            emit("execute", "blocked", result.error or "execution failed")

        # 8. conditional judge
        verdict_score, verdict_reason = 0.0, None
        if self._should_judge(agreement):
            verdict = self.provider.judge(question, safe_sql, schema_context)
            verdict_score, verdict_reason = verdict.score, verdict.reason
            metrics.inc("t2s_judge_calls_total")
            emit("judge", "ok", f"quality score {verdict_score:.2f}", {"reason": verdict_reason})
        else:
            verdict_score = agreement  # trust strong consensus without a judge call
            emit("judge", "skip", "strong consensus — judge call skipped")

        # 9. score
        conf = confidence.score(
            guardrail_ok=True, policy_ok=True, schema_ok=True,
            execution_ok=result.ok, consistency_fraction=agreement, judge_score=verdict_score)
        metrics.observe_confidence(conf.score)

        rows, row_count = result.rows, result.row_count
        if conf.score < self.settings.min_confidence_to_return_rows:
            rows, row_count = [], 0  # withhold low-confidence data
        emit("score", "ok", f"confidence {conf.score:.2f}")

        answer = Answer(
            question=question, sql=safe_sql, blocked=False,
            block_reason=None if result.ok else result.error,
            confidence=conf.score, signals=conf.signals,
            columns=result.columns, rows=rows, row_count=row_count,
            truncated=result.truncated, judge_reason=verdict_reason,
            tokens_used=self.provider.usage().total_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if self.cache is not None and result.ok:
            self.cache.set(cache_key, answer.to_dict())

        self._audit(request_id, answer)
        return answer

    def _cap_result(self, result: ExecutionResult) -> ExecutionResult:
        """Hard ceiling on the rows the pipeline will surface, enforced independently
        of the Database implementation and of the query's own LIMIT. Bounds data egress
        and payload size even if a Database port returns an unbounded result."""
        cap = self.settings.max_result_rows
        if result.ok and cap >= 0 and len(result.rows) > cap:
            return replace(result, rows=result.rows[:cap], row_count=cap, truncated=True)
        return result

    def _should_judge(self, agreement: float) -> bool:
        mode = self.settings.judge_mode
        if mode == "off":
            return False
        if mode == "always":
            return True
        return agreement < self.settings.conditional_judge_threshold

    def _blocked(self, question, sql, reason, signals, started, *, request_id="-") -> Answer:
        metrics.inc("t2s_blocked_total", reason=reason.split(":")[0])
        ans = Answer(question=question, sql=sql, blocked=True, block_reason=reason,
                     confidence=0.0, signals=signals,
                     tokens_used=self.provider.usage().total_tokens,
                     latency_ms=int((time.monotonic() - started) * 1000))
        self._audit(request_id, ans)
        return ans

    def _audit(self, request_id: str, ans: Answer) -> None:
        audit(logger, request_id=request_id, question=ans.question, sql=ans.sql,
              blocked=ans.blocked, block_reason=ans.block_reason,
              policy_ok=ans.signals.get("policy_ok", True),
              signals=ans.signals,
              confidence=ans.confidence, tokens_used=ans.tokens_used,
              latency_ms=ans.latency_ms, cached=ans.cached)
