"""Observability: structured logs, an audit trail, and metrics.

- Structured JSON logs so a log aggregator (Loki, CloudWatch, Datadog) can index them.
- An audit event for every question: who asked, what SQL was chosen, whether it was
  blocked and why, the confidence, and token cost. For a data-access tool this trail
  is a compliance requirement, not a nicety. It is emitted as a log line (shippable
  anywhere) rather than written by the read-only DB role.
- A tiny in-process metrics registry exposed in Prometheus text format at /metrics.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import defaultdict
from threading import Lock

_configured = False


def configure_logging(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    _configured = True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra_fields"):
            payload.update(record.extra_fields)  # type: ignore[attr-defined]
        return json.dumps(payload, default=str)


def audit(logger: logging.Logger, **fields) -> None:
    """Emit one structured audit event."""
    rec = logger.makeRecord(logger.name, logging.INFO, __file__, 0, "audit", (), None)
    rec.extra_fields = fields
    logger.handle(rec)


class Metrics:
    """Minimal thread-safe counters + a confidence histogram."""

    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self._conf_buckets = [0.2, 0.4, 0.6, 0.8, 1.01]
        self._conf_hist: dict[float, int] = defaultdict(int)
        self._lock = Lock()

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] += value

    def observe_confidence(self, value: float) -> None:
        with self._lock:
            for b in self._conf_buckets:
                if value <= b:
                    self._conf_hist[b] += 1
                    break

    def render(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            for (name, labels), val in sorted(self._counters.items()):
                label_str = ",".join(f'{k}="{v}"' for k, v in labels)
                suffix = f"{{{label_str}}}" if label_str else ""
                lines.append(f"{name}{suffix} {val}")
            for b in self._conf_buckets:
                lines.append(f'confidence_bucket{{le="{b}"}} {self._conf_hist[b]}')
        return "\n".join(lines) + "\n"


metrics = Metrics()
