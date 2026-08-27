"""Calibration metrics: does the reported confidence track actual correctness?

Given (confidence, correct) pairs for the ANSWERED cases, compute:
  * reliability bins  -- per confidence range: count, mean confidence, accuracy
  * ECE               -- Expected Calibration Error (lower is better, 0 = perfect)
  * Brier score       -- mean squared error of confidence vs 0/1 outcome (lower better)

Pure functions, no I/O, so they unit-test cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Bin:
    lo: float
    hi: float
    count: int = 0
    conf_sum: float = 0.0
    correct: int = 0

    @property
    def mean_confidence(self) -> float:
        return self.conf_sum / self.count if self.count else 0.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.count if self.count else 0.0

    @property
    def gap(self) -> float:
        return abs(self.mean_confidence - self.accuracy) if self.count else 0.0


@dataclass
class CalibrationReport:
    n: int = 0
    ece: float = 0.0
    brier: float = 0.0
    bins: list[Bin] = field(default_factory=list)


def _make_bins(n_bins: int) -> list[Bin]:
    edges = [i / n_bins for i in range(n_bins + 1)]
    return [Bin(lo=edges[i], hi=edges[i + 1]) for i in range(n_bins)]


def _bin_index(conf: float, n_bins: int) -> int:
    idx = int(conf * n_bins)
    return min(max(idx, 0), n_bins - 1)


def compute_calibration(pairs: list[tuple[float, bool]], *, n_bins: int = 5) -> CalibrationReport:
    """pairs = [(confidence, correct), ...] for ANSWERED cases only."""
    report = CalibrationReport(bins=_make_bins(n_bins))
    if not pairs:
        return report

    report.n = len(pairs)

    brier_sum = 0.0
    for conf, correct in pairs:
        outcome = 1.0 if correct else 0.0
        brier_sum += (conf - outcome) ** 2
        b = report.bins[_bin_index(conf, n_bins)]
        b.count += 1
        b.conf_sum += conf
        b.correct += 1 if correct else 0
    report.brier = brier_sum / len(pairs)

    ece = 0.0
    for b in report.bins:
        if b.count:
            ece += (b.count / report.n) * b.gap
    report.ece = ece

    return report


def pairs_from_results(results) -> list[tuple[float, bool]]:
    """Pull (confidence, correct) from CaseResults, ANSWERED cases only."""
    out: list[tuple[float, bool]] = []
    for r in results:
        if r.expected_block or r.got_block:
            continue
        out.append((float(r.confidence), bool(r.correct)))
    return out
