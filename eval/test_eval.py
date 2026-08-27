"""Regression guard: the offline harness must keep passing.

This runs the REAL pipeline over the labeled dataset with the deterministic
FakeProvider + SQLite backend (no network, no API key), so it is safe for CI. It
asserts execution accuracy stays high and that the governance (block) cases are
refused -- catching any future change that breaks the pipeline end to end.
"""

from pathlib import Path

from eval.calibration import compute_calibration, pairs_from_results
from eval.dataset import load_cases
from eval.runner import run_all

_DATASET = Path(__file__).parent / "dataset.jsonl"
_DENIED = ["customers.ssn", "customers.phone"]


def _summary():
    return run_all(load_cases(_DATASET), _DENIED)


def test_offline_accuracy_is_perfect():
    s = _summary()
    assert s.total > 0
    assert s.accuracy == 1.0, [
        (r.id, r.reason) for r in s.results if not r.correct
    ]


def test_block_cases_are_refused():
    s = _summary()
    block_results = [r for r in s.results if r.expected_block]
    assert block_results, "dataset should contain block cases"
    for r in block_results:
        assert r.got_block, f"{r.id} should have been blocked but was answered"


def test_answer_cases_are_answered():
    s = _summary()
    for r in s.results:
        if not r.expected_block:
            assert not r.got_block, f"{r.id} was wrongly blocked: {r.reason}"


def test_calibration_metrics_are_computable():
    s = _summary()
    cal = compute_calibration(pairs_from_results(s.results), n_bins=5)
    assert cal.n > 0
    assert 0.0 <= cal.ece <= 1.0
    assert 0.0 <= cal.brier <= 1.0
