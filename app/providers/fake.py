"""A deterministic in-memory provider for tests and offline demos.

Lets the whole pipeline run end-to-end with no API key: you supply canned SQL for
generate() and a fixed verdict for judge().
"""

from __future__ import annotations

from ..ports import JudgeVerdict, Usage


class FakeProvider:
    def __init__(
        self,
        candidates: list[str] | None = None,
        verdict: JudgeVerdict | None = None,
    ) -> None:
        self._candidates = candidates or ["SELECT 1"]
        self._verdict = verdict or JudgeVerdict(True, 0.9, "looks correct")
        self._usage = Usage()

    def generate(self, question: str, schema_context: str, k: int) -> list[str]:
        self._usage.calls += k
        # cycle through provided candidates to fill k slots
        return [self._candidates[i % len(self._candidates)] for i in range(k)]

    def judge(self, question: str, sql: str, schema_context: str) -> JudgeVerdict:
        self._usage.calls += 1
        return self._verdict

    def usage(self) -> Usage:
        return self._usage
