"""CLI entry point: python -m eval.run [--live] [--plot]"""

from __future__ import annotations

import sys
from pathlib import Path

from .calibration import compute_calibration, pairs_from_results
from .dataset import load_cases
from .report import format_report
from .runner import run_all

DENIED_COLUMNS = ["customers.ssn", "customers.phone"]
_DATASET = Path(__file__).parent / "dataset.jsonl"
_PLOT_PATH = Path(__file__).parent / "reliability.png"


def main() -> None:
    cases = load_cases(_DATASET)

    if "--live" in sys.argv:
        from .runner_live import run_all_live
        summary = run_all_live(cases, DENIED_COLUMNS)   # real Groq; needs a key
    else:
        summary = run_all(cases, DENIED_COLUMNS)        # offline, deterministic

    pairs = pairs_from_results(summary.results)
    cal = compute_calibration(pairs, n_bins=5)

    print()
    print(format_report(summary.accuracy, summary.correct, summary.total, cal))
    print()

    for r in summary.results:
        mark = "PASS" if r.correct else "FAIL"
        kind = "block" if r.expected_block else "answer"
        line = f"  [{mark}] {r.id}  ({kind})"
        if not r.correct:
            line += f"  -- {r.reason}"
        print(line)
    print()

    if "--plot" in sys.argv:
        from .plot import save_reliability_plot
        out = save_reliability_plot(cal, _PLOT_PATH)
        print(f"Saved reliability diagram -> {out}\n")


if __name__ == "__main__":
    main()
