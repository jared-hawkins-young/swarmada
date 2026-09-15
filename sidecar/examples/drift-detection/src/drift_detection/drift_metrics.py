"""Drift-detection-specific Prometheus metrics.

Registered on import; scraped from the foundation `/metrics` endpoint
(same Prometheus registry). These are the metrics that only matter to
drift detection — the LLM call latency / gateway failure counters live
in the foundation `swarmada_sidecar.metrics` module.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --- Counters ---

drift_alerts_total = Counter(
    "sidecar_drift_alerts_total",
    "Total number of drift alerts emitted, by robot / model / task_type.",
    labelnames=("robot_id", "model_id", "task_type"),
)

diagnoses_pruned_total = Counter(
    "sidecar_diagnoses_pruned_total",
    "Total diagnoses removed from local store by the retention pruner.",
)

submissions_deduped_total = Counter(
    "sidecar_submissions_deduped_total",
    "Total incoming TaskCompletionResult submissions rejected as duplicates.",
)

# --- Histograms ---

diagnosis_pipeline_latency_seconds = Histogram(
    "sidecar_diagnosis_pipeline_latency_seconds",
    "End-to-end drift-diagnosis pipeline latency (from drift event to persist).",
    buckets=(0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0),
)

# --- Gauges ---

rolling_confidence = Gauge(
    "sidecar_rolling_confidence",
    "Rolling mean confidence for a (robot, model, task_type) tuple.",
    labelnames=("robot_id", "model_id", "task_type"),
)

rolling_failure_rate = Gauge(
    "sidecar_rolling_failure_rate",
    "Rolling failure rate for a (robot, model, task_type) tuple.",
    labelnames=("robot_id", "model_id", "task_type"),
)

rolling_kl_divergence = Gauge(
    "sidecar_rolling_kl_divergence",
    "Rolling KL divergence vs reference for a (robot, model, task_type) tuple.",
    labelnames=("robot_id", "model_id", "task_type"),
)

diagnosis_row_count = Gauge(
    "sidecar_diagnosis_row_count",
    "Current number of persisted diagnoses in the local store.",
)


__all__ = [
    "drift_alerts_total",
    "diagnoses_pruned_total",
    "submissions_deduped_total",
    "diagnosis_pipeline_latency_seconds",
    "rolling_confidence",
    "rolling_failure_rate",
    "rolling_kl_divergence",
    "diagnosis_row_count",
]
