"""Draw a reliability diagram (calibration curve) and save it as a PNG.

matplotlib is imported lazily so the rest of the harness runs without it; only
plotting needs it.
"""

from __future__ import annotations

from pathlib import Path

from .calibration import CalibrationReport


def save_reliability_plot(cal: CalibrationReport, path: str | Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")  # headless backend: no display needed (works in CI)
    import matplotlib.pyplot as plt

    path = Path(path)

    xs = [b.mean_confidence for b in cal.bins if b.count]
    ys = [b.accuracy for b in cal.bins if b.count]
    sizes = [b.count for b in cal.bins if b.count]

    fig, ax = plt.subplots(figsize=(5, 5))

    ax.plot([0, 1], [0, 1], linestyle="--", color="grey", label="perfect calibration")

    if xs:
        ax.scatter(xs, ys, s=[40 + 30 * n for n in sizes], color="#2a6fdb",
                   zorder=3, label="observed bins")
        ax.plot(xs, ys, color="#2a6fdb", alpha=0.4, zorder=2)

    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Mean predicted confidence")
    ax.set_ylabel("Actual accuracy")
    ax.set_title(f"Reliability diagram  (ECE={cal.ece:.3f}, Brier={cal.brier:.3f})")
    ax.legend(loc="upper left", fontsize=8)
    ax.set_aspect("equal")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
