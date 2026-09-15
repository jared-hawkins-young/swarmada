"""Drift-detection-specific configuration.

Extends the foundation `swarmada_sidecar.config.Config` with the knobs
that only matter for the drift-detection agent (rolling window bounds,
drift thresholds, snapshot cadence, retention windows).

Uses its own env prefix `SIDECAR_DRIFT_` so the foundation Config can
keep `extra="forbid"` (fail-fast on typos). e.g. the max-samples knob
is `SIDECAR_DRIFT_MAX_SAMPLES`, not `SIDECAR_MAX_SAMPLES`.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DriftConfig(BaseSettings):
    """Drift-detection runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="SIDECAR_DRIFT_",
        env_file=None,
        case_sensitive=False,
        extra="forbid",
    )

    # --- Rolling window bounds ---
    max_samples: int = Field(default=200, ge=10, le=10_000)
    time_cap_hours: int = Field(default=24, ge=1, le=168)
    min_sample_count: int = Field(default=50, ge=10)
    sustained_samples: int = Field(default=20, ge=1)

    # --- Drift thresholds ---
    min_mean_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    max_failure_rate: float = Field(default=0.15, ge=0.0, le=1.0)
    failure_confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    max_kl_divergence: float = Field(default=0.4, ge=0.0)

    # --- Snapshot cadence ---
    snapshot_interval_seconds: int = Field(default=60, ge=1)
    snapshot_interval_samples: int = Field(default=20, ge=1)

    # --- Retention ---
    diagnosis_retention_days: int = Field(default=30, ge=1)
    dedup_retention_hours: int = Field(default=24, ge=1)
    topk_class_predictions: int = Field(default=5, ge=0, le=50)


def load_drift_config() -> DriftConfig:
    """Load and validate drift-detection configuration."""
    return DriftConfig()  # type: ignore[call-arg]
