"""Foundation Prometheus metrics.

The foundation exports only the metrics that every LLM-reasoning agent
needs regardless of domain: LLM latency, gateway failure counts, guardrail
blocks, submission latency, and per-dependency health.

Domain-specific metrics (e.g. drift alerts, rolling confidence gauges)
live alongside their consumer — see
`examples/drift-detection/src/drift_detection/drift_metrics.py`.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --- Counters ---

guardrail_blocks_total = Counter(
    "sidecar_guardrail_blocks_total",
    "Total number of guardrail blocks, by check_type / reason.",
    labelnames=("check_type", "reason"),
)

llm_gateway_failures_total = Counter(
    "sidecar_llm_gateway_failures_total",
    "Total LiteLLM gateway failures (retries exhausted).",
    labelnames=("model", "reason"),
)

# --- Histograms ---

llm_call_latency_seconds = Histogram(
    "sidecar_llm_call_latency_seconds",
    "End-to-end latency of a LiteLLM call.",
    labelnames=("model",),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0),
)

submission_latency_seconds = Histogram(
    "sidecar_submission_latency_seconds",
    "Latency to accept an inbound submission at the RPC layer (server-side).",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
)

# --- Gauges ---

dependency_healthy = Gauge(
    "sidecar_dependency_healthy",
    "1.0 if the named dependency is healthy per last check, 0.0 otherwise.",
    labelnames=("dependency",),
)


__all__ = [
    "guardrail_blocks_total",
    "llm_gateway_failures_total",
    "llm_call_latency_seconds",
    "submission_latency_seconds",
    "dependency_healthy",
]
