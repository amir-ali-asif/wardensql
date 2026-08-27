"""Human-readable calibration + accuracy report (plain text)."""

from __future__ import annotations

from .calibration import CalibrationReport


def format_report(accuracy: float, correct: int, total: int,
                  cal: CalibrationReport) -> str:
    lines: list[str] = []
    lines.append("=" * 52)
    lines.append(" Text-to-SQL — Evaluation Report")
    lines.append("=" * 52)
    lines.append(f" Execution accuracy : {correct}/{total} = {accuracy:.1%}")
    lines.append(f" Answered (scored)  : {cal.n} cases")
    lines.append(f" ECE (lower=better) : {cal.ece:.3f}")
    lines.append(f" Brier (lower=better): {cal.brier:.3f}")
    lines.append("")
    lines.append(" Reliability bins (confidence vs actual accuracy):")
    lines.append(f"   {'range':>11} {'n':>3} {'mean_conf':>10} {'accuracy':>9} {'gap':>6}")
    for b in cal.bins:
        if b.count == 0:
            continue
        rng = f"{b.lo:.1f}-{b.hi:.1f}"
        lines.append(f"   {rng:>11} {b.count:>3} {b.mean_confidence:>10.3f} "
                     f"{b.accuracy:>9.3f} {b.gap:>6.3f}")
    lines.append("=" * 52)
    return "\n".join(lines)
