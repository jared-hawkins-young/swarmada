"""Drift detector.

Evaluates a RollingWindow against three configured thresholds:
1. Mean-confidence threshold sustained across the last N samples.
2. Failure-rate threshold sustained across the last N samples.
3. KL divergence between current class distribution and a reference.

Emits DriftEvent when any threshold trips. Respects min_sample_count
bootstrap gate. Fail-closed: if reference distribution is missing for KL,
that specific check is skipped and logged; the other two still evaluate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

from drift_detection.aggregator import RollingWindow, WindowKey
from drift_detection.drift_config import DriftConfig

log = structlog.get_logger(__name__)


class DriftReason(str, Enum):
    MEAN_CONFIDENCE = "MEAN_CONFIDENCE"
    FAILURE_RATE = "FAILURE_RATE"
    KL_DIVERGENCE = "KL_DIVERGENCE"


@dataclass
class DriftEvent:
    window_key: WindowKey
    triggered_at_epoch: float
    reasons: list[DriftReason]
    window_snapshot: dict[str, Any] = field(default_factory=dict)
    triggering_fleet_action_id: str | None = None


class DriftDetector:
    """Threshold evaluator."""

    def __init__(self, cfg: DriftConfig) -> None:
        self._cfg = cfg
        # Reference distributions per window key. Bootstrap policy: first
        # N samples after a model rollout complete populate this. For MVP,
        # the reference is set once at first-fill and never updated.
        self._reference: dict[WindowKey, dict[str, float]] = {}

    def evaluate(self, window: RollingWindow) -> DriftEvent | None:
        """Return a DriftEvent if the window's state trips any threshold,
        else None."""
        if window.sample_count() < self._cfg.min_sample_count:
            return None

        last = window.last_n(self._cfg.sustained_samples)
        if len(last) < self._cfg.sustained_samples:
            return None

        reasons: list[DriftReason] = []

        # 1) Mean confidence in the sustained tail.
        tail_conf = sum(s.confidence for s in last) / len(last)
        if tail_conf < self._cfg.min_mean_confidence:
            reasons.append(DriftReason.MEAN_CONFIDENCE)

        # 2) Failure rate in the sustained tail.
        tail_failures = sum(
            1 for s in last if s.confidence < self._cfg.failure_confidence_threshold
        )
        tail_failure_rate = tail_failures / len(last)
        if tail_failure_rate > self._cfg.max_failure_rate:
            reasons.append(DriftReason.FAILURE_RATE)

        # 3) KL divergence vs reference (skipped fail-closed if no reference).
        reference = self._reference.get(window.key)
        if reference is not None:
            kl = window.kl_divergence(reference)
            window.emit_kl_metric(kl)
            if kl > self._cfg.max_kl_divergence:
                reasons.append(DriftReason.KL_DIVERGENCE)
        else:
            # First-fill: bootstrap the reference at min_sample_count so
            # subsequent windows have something to compare against.
            self._maybe_bootstrap_reference(window)

        if not reasons:
            return None

        import time as _time

        return DriftEvent(
            window_key=window.key,
            triggered_at_epoch=_time.time(),
            reasons=reasons,
            window_snapshot=window.snapshot(),
            triggering_fleet_action_id=window.most_recent_action_id(),
        )

    def _maybe_bootstrap_reference(self, window: RollingWindow) -> None:
        """First-fill policy: freeze the reference distribution the first
        time a window reaches min_sample_count with a non-empty class
        histogram. Future refinements can replace this with an operator-
        provided reference."""
        if window.key in self._reference:
            return
        probs = window.agg.normalized_class_probabilities()
        if not probs:
            return
        self._reference[window.key] = probs
        log.info(
            "drift.reference_bootstrapped",
            key=window.key,
            classes=len(probs),
        )
