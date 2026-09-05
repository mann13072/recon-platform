"""Metrics (spec section 60).

Two families, and the distinction matters more than the implementation:

* **technical metrics** tell an engineer whether the platform is working;
* **financial-control metrics** tell a controller whether the *output* can be
  trusted.

A platform with perfect uptime and a rising false-match rate is failing, and
only the second family would show it. So the automation rate is never exported
without the false-match rate beside it.

The registry is intentionally small and dependency-free. Exporting to
Prometheus, OpenTelemetry or a hosted service is a deployment concern; the
metric *names and meanings* are a product concern and live here.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "FINANCIAL_CONTROL_METRICS",
    "TECHNICAL_METRICS",
    "MetricKind",
    "MetricRegistry",
    "registry",
]


class MetricKind(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    name: str
    kind: MetricKind
    description: str
    unit: str = ""


# Spec section 60, technical metrics.
TECHNICAL_METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        "connector_sync_success_rate",
        MetricKind.GAUGE,
        "Fraction of connector syncs that completed successfully.",
    ),
    MetricDefinition(
        "connector_sync_latency",
        MetricKind.HISTOGRAM,
        "Wall-clock duration of a connector sync.",
        "seconds",
    ),
    MetricDefinition(
        "ingestion_failures", MetricKind.COUNTER, "Files that could not be ingested, by reason."
    ),
    MetricDefinition(
        "normalization_error_rate", MetricKind.GAUGE, "Fraction of source rows that failed to map."
    ),
    MetricDefinition(
        "matching_job_duration",
        MetricKind.HISTOGRAM,
        "End-to-end duration of a reconciliation run.",
        "seconds",
    ),
    MetricDefinition(
        "candidate_explosion_count",
        MetricKind.COUNTER,
        "Runs where candidates per transaction exceeded the expected band. "
        "A rising value means a blocking key stopped discriminating.",
    ),
    MetricDefinition(
        "rule_conflicts",
        MetricKind.COUNTER,
        "Anchors where two records satisfied the same rule equally.",
    ),
    MetricDefinition(
        "ai_call_error_rate", MetricKind.GAUGE, "Fraction of AI calls that failed at the transport."
    ),
    MetricDefinition(
        "ai_schema_validation_failure",
        MetricKind.COUNTER,
        "AI responses rejected by their schema. Each produced no state change.",
    ),
    MetricDefinition(
        "database_latency", MetricKind.HISTOGRAM, "Query duration by operation.", "seconds"
    ),
    MetricDefinition("queue_depth", MetricKind.GAUGE, "Pending background jobs."),
)

# Spec section 60, financial-control metrics.
FINANCIAL_CONTROL_METRICS: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        "auto_match_precision",
        MetricKind.GAUGE,
        "Of automatic matches later reviewed, the fraction confirmed correct. "
        "The single most important number in the platform.",
    ),
    MetricDefinition(
        "false_automatic_match_rate",
        MetricKind.GAUGE,
        "Automatic matches later found to be wrong, as a fraction of all "
        "automatic matches. Never publish the automation rate without this.",
    ),
    MetricDefinition(
        "manual_override_rate",
        MetricKind.GAUGE,
        "Fraction of automated decisions a human overrode.",
    ),
    MetricDefinition(
        "reopened_reconciliations", MetricKind.COUNTER, "Reconciliations reopened after closing."
    ),
    MetricDefinition("exception_aging", MetricKind.HISTOGRAM, "Age of open exceptions.", "days"),
    MetricDefinition(
        "high_value_unmatched_count",
        MetricKind.GAUGE,
        "Unmatched transactions at or above the materiality threshold.",
    ),
    MetricDefinition(
        "rule_change_count",
        MetricKind.COUNTER,
        "Rule and threshold changes, by whether they weakened a control.",
    ),
    MetricDefinition(
        "auto_match_rate",
        MetricKind.GAUGE,
        "Fraction of matches made automatically. Meaningless on its own.",
    ),
)

ALL_METRICS = TECHNICAL_METRICS + FINANCIAL_CONTROL_METRICS
_BY_NAME = {m.name: m for m in ALL_METRICS}

MetricLabels = tuple[tuple[str, str], ...]
MetricKey = tuple[str, MetricLabels]


@dataclass
class MetricRegistry:
    """A tiny thread-safe in-process registry.

    Labels are a sorted tuple so the same label set always produces the same
    key regardless of the order the caller passed them.
    """

    _counters: dict[MetricKey, float] = field(default_factory=lambda: defaultdict(float))
    _gauges: dict[MetricKey, float] = field(default_factory=dict)
    _histograms: dict[MetricKey, list[float]] = field(default_factory=lambda: defaultdict(list))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _key(self, name: str, labels: dict[str, str] | None) -> MetricKey:
        if name not in _BY_NAME:
            raise KeyError(
                f"unknown metric '{name}'. Metrics are declared in "
                "packages/observability/metrics.py so that names cannot drift."
            )
        return (name, tuple(sorted((labels or {}).items())))

    def increment(self, name: str, value: float = 1.0, **labels: str) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += value

    def set(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    def observe(self, name: str, value: float, **labels: str) -> None:
        with self._lock:
            self._histograms[self._key(name, labels)].append(value)

    @contextmanager
    def timer(self, name: str, **labels: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, time.perf_counter() - started, **labels)

    def snapshot(self) -> dict[str, object]:
        """Everything recorded so far, for an exporter or a test."""
        with self._lock:
            return {
                "counters": {_render(key): value for key, value in self._counters.items()},
                "gauges": {_render(key): value for key, value in self._gauges.items()},
                "histograms": {
                    _render(key): _summarise(values) for key, values in self._histograms.items()
                },
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


def _render(key: MetricKey) -> str:
    name, labels = key
    if not labels:
        return str(name)
    rendered = ",".join(f"{k}={v}" for k, v in labels)
    return f"{name}{{{rendered}}}"


def _summarise(values: list[float]) -> dict[str, float]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    count = len(ordered)

    def percentile(fraction: float) -> float:
        index = min(count - 1, int(fraction * count))
        return round(ordered[index], 6)

    return {
        "count": count,
        "sum": round(sum(ordered), 6),
        "min": round(ordered[0], 6),
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": round(ordered[-1], 6),
    }


registry = MetricRegistry()
