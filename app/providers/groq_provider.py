"""Groq (and any OpenAI-compatible) provider.

- Parallel sampling: the k self-consistency calls run concurrently in a thread pool,
  so five samples cost roughly one sample's latency instead of five.
- Usage accounting: sums token usage and captures the x-ratelimit-remaining-* headers
  so you always know how much free-tier budget is left.
- Retry: relies on the SDK's built-in backoff for 429/5xx, surfaced cleanly upstream.

The `groq` SDK is imported lazily so the rest of the app (and the test suite) does
not require it to be installed.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from ..ports import JudgeVerdict, Usage

_GEN_SYSTEM = (
    "You are a careful PostgreSQL query writer. Given a schema and a question, "
    "output ONE read-only SQL SELECT that answers it. Use only tables and columns "
    "in the schema; never invent identifiers; never write to the database. "
    "Return ONLY the SQL -- no prose, no markdown fences."
)
_JUDGE_SYSTEM = (
    "You are a strict SQL reviewer. Given a schema, a question, and a candidate "
    "PostgreSQL query, decide whether it correctly and completely answers the "
    "question. Respond with ONLY JSON: "
    '{"answers_question": true|false, "score": 0.0-1.0, "reason": "one sentence"}'
)


def _strip_sql(text: str) -> str:
    text = (text or "").strip()
    fence = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1)
    return text.strip().rstrip(";").strip()


class GroqProvider:
    def __init__(self, settings) -> None:
        # print("DEBUG base_url:", settings.base_url)
        from groq import Groq  # lazy import
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is not set. Get a free key at console.groq.com")
        self._settings = settings
        self._client = Groq(
            api_key=settings.groq_api_key,
            # base_url=settings.base_url,
            max_retries=settings.max_retries,
        )
        self._usage = Usage()

    def _extra_body(self) -> dict:
        # reasoning_effort is honoured by the gpt-oss models; harmless config knob.
        if self._settings.reasoning_effort:
            return {"reasoning_effort": self._settings.reasoning_effort}
        return {}

    def _complete(self, system: str, user: str, temperature: float) -> str:
        raw = self._client.chat.completions.with_raw_response.create(
            model=self._settings.model,
            temperature=temperature,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            extra_body=self._extra_body(),
        )
        # Capture remaining budget from headers for observability.
        headers = getattr(raw, "headers", {}) or {}
        self._record_headers(headers)
        completion = raw.parse()
        usage = getattr(completion, "usage", None)
        if usage is not None:
            self._usage.total_tokens += getattr(usage, "total_tokens", 0) or 0
        self._usage.calls += 1
        return completion.choices[0].message.content or ""

    def _record_headers(self, headers) -> None:
        def _to_int(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None
        get = headers.get if hasattr(headers, "get") else (lambda k: None)
        self._usage.remaining_requests = _to_int(get("x-ratelimit-remaining-requests"))
        self._usage.remaining_tokens = _to_int(get("x-ratelimit-remaining-tokens"))

    def _alias_rules(self) -> str:
        """Optional, purely stylistic alias suggestions for the prompt.

        Security does NOT depend on these: app/policy.py + app/resolver.py resolve
        *any* valid alias to its canonical table.column and enforce policy on that
        identity. The model is free to use arbitrary aliases; suggesting a house
        style just keeps generated SQL tidy and consistent. Empty map = no hint.
        """
        aliases = getattr(self._settings, "table_aliases", None) or {}
        if not aliases:
            return ""
        lines = "\n".join(f"{t} AS {a}" for t, a in aliases.items())
        return (
            "\n\nSuggested table aliases (a house style -- any valid alias is fine):\n"
            f"{lines}\n"
        )

    def generate(self, question: str, schema_context: str, k: int) -> list[str]:
        prompt = (
            f"Schema:\n{schema_context}"
            f"{self._alias_rules()}"
            f"\n\nQuestion: {question}\n\nSQL:"
        )

        def one(_):
            return _strip_sql(self._complete(_GEN_SYSTEM, prompt, self._settings.temperature))

        with ThreadPoolExecutor(max_workers=min(k, 8)) as pool:
            return list(pool.map(one, range(k)))

    def judge(self, question: str, sql: str, schema_context: str) -> JudgeVerdict:
        prompt = f"Schema:\n{schema_context}\n\nQuestion: {question}\n\nCandidate SQL:\n{sql}"
        raw = self._complete(_JUDGE_SYSTEM, prompt, 0.0)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            data = json.loads(match.group(0) if match else raw)
            return JudgeVerdict(
                answers_question=bool(data.get("answers_question", False)),
                score=float(data.get("score", 0.0)),
                reason=str(data.get("reason", "")),
            )
        except (json.JSONDecodeError, AttributeError, ValueError):
            return JudgeVerdict(False, 0.0, "judge returned unparseable output")

    def usage(self) -> Usage:
        return self._usage
