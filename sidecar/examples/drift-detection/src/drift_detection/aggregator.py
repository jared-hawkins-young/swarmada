"""Rolling window store for drift-detection.

Windows live in memory keyed by (robot_id, model_id, model_version, task_type).
Periodic snapshots to SQLite for crash recovery.

Fail-closed: sample append raises on schema violation; snapshot failures are
raised (caller decides retry).
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from drift_detection.drift_config import DriftConfig
from drift_detection.drift_metrics import (
    rolling_confidence,
    rolling_failure_rate,
    rolling_kl_divergence,
)
from drift_detection.store import DriftStore


WindowKey = tuple[str, str, str, str]  # (robot_id, model_id, model_version, task_type)


@dataclass
class TaskSample:
    """One structured task-completion sample (in-memory representation)."""

    robot_id: str
    fleet_action_id: str
    model_id: str
    model_version: str
    confidence: float
    class_predictions: dict[str, float] = field(default_factory=dict)
    captured_at_epoch: float = 0.0
    task_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "fleet_action_id": self.fleet_action_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "confidence": self.confidence,
            "class_predictions": self.class_predictions,
            "captured_at_epoch": self.captured_at_epoch,
            "task_type": self.task_type,
        }


@dataclass
class WindowAggregates:
    """Cached running aggregates for a RollingWindow. O(1) reads."""

    sum_confidence: float = 0.0
    count: int = 0
    failure_count: int = 0
    class_histogram: dict[str, int] = field(default_factory=dict)

    def mean_confidence(self) -> float:
        return self.sum_confidence / self.count if self.count else 0.0

    def failure_rate(self) -> float:
        return self.failure_count / self.count if self.count else 0.0

    def normalized_class_probabilities(self) -> dict[str, float]:
        total = sum(self.class_histogram.values())
        if not total:
            return {}
        return {k: v / total for k, v in self.class_histogram.items()}


class RollingWindow:
    """Bounded sliding window for a (robot, model, version, task_type) tuple."""

    def __init__(
        self,
        *,
        key: WindowKey,
        max_samples: int,
        time_cap_seconds: float,
        failure_confidence_threshold: float,
    ) -> None:
        self.key = key
        self._max_samples = max_samples
        self._time_cap_seconds = time_cap_seconds
        self._failure_threshold = failure_confidence_threshold
        self._samples: deque[TaskSample] = deque()
        self.agg = WindowAggregates()
        self._samples_since_snapshot = 0

    # --- core append ---

    def add(self, sample: TaskSample) -> None:
        self._evict_by_time()
        self._samples.append(sample)
        self._apply_agg_delta(sample, sign=+1)
        while len(self._samples) > self._max_samples:
            evicted = self._samples.popleft()
            self._apply_agg_delta(evicted, sign=-1)
        self._samples_since_snapshot += 1
        self._emit_metrics()

    def _evict_by_time(self) -> None:
        cutoff = time.time() - self._time_cap_seconds
        while self._samples and self._samples[0].captured_at_epoch < cutoff:
            evicted = self._samples.popleft()
            self._apply_agg_delta(evicted, sign=-1)

    def _apply_agg_delta(self, sample: TaskSample, *, sign: int) -> None:
        self.agg.sum_confidence += sign * sample.confidence
        self.agg.count += sign
        is_failure = sample.confidence < self._failure_threshold
        if is_failure:
            self.agg.failure_count += sign
        for label in sample.class_predictions:
            current = self.agg.class_histogram.get(label, 0)
            new = current + sign
            if new <= 0:
                self.agg.class_histogram.pop(label, None)
            else:
                self.agg.class_histogram[label] = new

    # --- reads ---

    def sample_count(self) -> int:
        return self.agg.count

    def last_n(self, n: int) -> list[TaskSample]:
        if n <= 0:
            return []
        return list(self._samples)[-n:]

    def most_recent_action_id(self) -> str | None:
        return self._samples[-1].fleet_action_id if self._samples else None

    def kl_divergence(self, reference: dict[str, float]) -> float:
        """Compute KL(P || Q) where P is the current class distribution and
        Q is the reference. Returns 0.0 if reference is empty (no signal).
        Uses natural log."""
        if not reference:
            return 0.0
        current = self.agg.normalized_class_probabilities()
        if not current:
            return 0.0
        # Smooth zeros to avoid log(0).
        eps = 1e-9
        div = 0.0
        for label, p in current.items():
            q = reference.get(label, eps)
            div += p * math.log((p + eps) / (q + eps))
        return div

    # --- snapshot ---

    def snapshot(self) -> dict[str, Any]:
        return {
            "samples": [s.as_dict() for s in self._samples],
        }

    @classmethod
    def rehydrate(
        cls,
        *,
        key: WindowKey,
        snapshot: dict[str, Any],
        max_samples: int,
        time_cap_seconds: float,
        failure_confidence_threshold: float,
    ) -> "RollingWindow":
        w = cls(
            key=key,
            max_samples=max_samples,
            time_cap_seconds=time_cap_seconds,
            failure_confidence_threshold=failure_confidence_threshold,
        )
        for raw in snapshot.get("samples", []):
            w.add(
                TaskSample(
                    robot_id=raw["robot_id"],
                    fleet_action_id=raw["fleet_action_id"],
                    model_id=raw["model_id"],
                    model_version=raw["model_version"],
                    confidence=float(raw["confidence"]),
                    class_predictions={
                        k: float(v) for k, v in raw.get("class_predictions", {}).items()
                    },
                    captured_at_epoch=float(raw["captured_at_epoch"]),
                    task_type=raw["task_type"],
                )
            )
        return w

    def _emit_metrics(self) -> None:
        robot_id, model_id, _, task_type = self.key
        rolling_confidence.labels(
            robot_id=robot_id, model_id=model_id, task_type=task_type
        ).set(self.agg.mean_confidence())
        rolling_failure_rate.labels(
            robot_id=robot_id, model_id=model_id, task_type=task_type
        ).set(self.agg.failure_rate())

    def emit_kl_metric(self, kl: float) -> None:
        robot_id, model_id, _, task_type = self.key
        rolling_kl_divergence.labels(
            robot_id=robot_id, model_id=model_id, task_type=task_type
        ).set(kl)

    def take_snapshot_if_due(
        self, *, snapshot_interval_samples: int
    ) -> bool:
        due = self._samples_since_snapshot >= snapshot_interval_samples
        if due:
            self._samples_since_snapshot = 0
        return due


class AggregatorStore:
    """Registry of RollingWindow instances plus SQLite snapshot glue."""

    def __init__(self, *, cfg: DriftConfig, store: DriftStore) -> None:
        self._cfg = cfg
        self._store = store
        self._windows: dict[WindowKey, RollingWindow] = {}
        self._time_cap_seconds = cfg.time_cap_hours * 3600

    def hydrate_from_snapshots(self) -> None:
        for entry in self._store.load_all_window_snapshots():
            k = entry["_key"]
            key: WindowKey = (
                k["robot_id"],
                k["model_id"],
                k["model_version"],
                k["task_type"],
            )
            self._windows[key] = RollingWindow.rehydrate(
                key=key,
                snapshot=entry,
                max_samples=self._cfg.max_samples,
                time_cap_seconds=self._time_cap_seconds,
                failure_confidence_threshold=self._cfg.failure_confidence_threshold,
            )

    def add_sample(self, sample: TaskSample) -> RollingWindow:
        key: WindowKey = (
            sample.robot_id,
            sample.model_id,
            sample.model_version,
            sample.task_type,
        )
        window = self._windows.get(key)
        if window is None:
            window = RollingWindow(
                key=key,
                max_samples=self._cfg.max_samples,
                time_cap_seconds=self._time_cap_seconds,
                failure_confidence_threshold=self._cfg.failure_confidence_threshold,
            )
            self._windows[key] = window
        window.add(sample)
        if window.take_snapshot_if_due(
            snapshot_interval_samples=self._cfg.snapshot_interval_samples
        ):
            self._persist_snapshot(window)
        return window

    def _persist_snapshot(self, window: RollingWindow) -> None:
        robot_id, model_id, model_version, task_type = window.key
        self._store.upsert_window_snapshot(
            robot_id=robot_id,
            model_id=model_id,
            model_version=model_version,
            task_type=task_type,
            snapshot=window.snapshot(),
        )

    def get_window(self, key: WindowKey) -> RollingWindow | None:
        return self._windows.get(key)

    def all_windows(self) -> list[RollingWindow]:
        return list(self._windows.values())
