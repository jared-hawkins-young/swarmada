# Phase 1 Data Model: Model Drift Detection

## Entities

### TaskCompletionResult

The unit of input to the sidecar. Emitted by the Swarmada Go control plane for every action a
robot completes (successfully or unsuccessfully).

| Field                | Type                | Constraints                                                              | Notes                                                          |
|----------------------|---------------------|--------------------------------------------------------------------------|----------------------------------------------------------------|
| `robot_id`           | string              | Non-empty. Format matches Swarmada Robot CR name.                        | Correlation ID.                                                |
| `fleet_action_id`    | string              | Non-empty. Unique per action across the cluster.                         | Correlation ID + dedup key.                                    |
| `model_id`           | string              | Non-empty. Matches `Robot.spec.installedModels[].name` in Swarmada.      | e.g. "pick-model".                                             |
| `model_version`      | string (semver)     | Matches `\d+\.\d+\.\d+(-[A-Za-z0-9.]+)?` per Swarmada `ModelRollout`.    | e.g. "1.4.2".                                                  |
| `confidence`         | float               | 0.0 ≤ value ≤ 1.0.                                                       | On-robot model's own confidence in the outcome.                |
| `class_predictions`  | list of ClassScore  | 0 to K items (K configurable, default 5). Optional.                      | Top-k predicted classes with scores summing to ≤ 1.0.          |
| `captured_at`        | timestamp (RFC3339) | Not in the future by more than 1 minute (clock skew tolerance).          | When the action completed on-robot.                            |
| `task_type`          | string              | Non-empty. Bounded enum sourced from `Robot.status.supportedActions`.    | e.g. "grip", "navigate", "handoff".                            |

**Uniqueness**: `fleet_action_id` is the dedup key. Duplicate submissions within a configurable
retention window (default 24h) are logged and dropped.

### ClassScore

Sub-entity of `TaskCompletionResult`. Represents one of the top-k class predictions.

| Field    | Type   | Constraints           | Notes                              |
|----------|--------|-----------------------|------------------------------------|
| `label`  | string | Non-empty. Bounded ≤ 128 chars.  | Class name from the robot's model. |
| `score`  | float  | 0.0 ≤ value ≤ 1.0.    | Softmax-normalized probability.    |

### RollingWindow

In-memory (with periodic snapshots) bounded sliding buffer per unique tuple
`(robot_id, model_id, model_version, task_type)`.

| Field                       | Type                     | Notes                                                             |
|-----------------------------|--------------------------|-------------------------------------------------------------------|
| `key`                       | 4-tuple                  | Composite key (see above).                                        |
| `samples`                   | deque of TaskCompletion  | Bounded by `max_samples` (default 200).                           |
| `oldest_at`                 | timestamp                | Wall-clock age gate: window ejects samples older than `time_cap`. |
| `mean_confidence`           | float                    | Running aggregate for O(1) drift-check reads.                     |
| `failure_count`             | int                      | Running count of samples below a configurable failure threshold.  |
| `class_histogram`           | map[string, int]         | Running class-frequency table.                                    |
| `snapshotted_at`            | timestamp (nullable)     | Most recent SQLite snapshot time.                                 |

**State transitions**: `bootstrapping` → `active` → `drifting` → `active`.
- `bootstrapping`: below `min_sample_count` (default 50). No drift checks.
- `active`: at or above `min_sample_count`. Drift checks run on each new sample.
- `drifting`: at least one threshold crossed for `sustained_samples` (default 20) consecutive samples. Emits `DriftEvent`.
- After emitting, returns to `active`. Re-entry into `drifting` requires the sustain condition to be met again.

### DriftEvent

Emitted by `drift_detector` when a `RollingWindow` transitions to `drifting`. Input to the
LangGraph state machine.

| Field           | Type                          | Notes                                                        |
|-----------------|-------------------------------|--------------------------------------------------------------|
| `window_key`    | 4-tuple                       | Same as `RollingWindow.key`.                                 |
| `triggered_at`  | timestamp                     | When drift fired.                                            |
| `reasons`       | list of DriftReason (enum)    | Which thresholds crossed: `MEAN_CONFIDENCE`, `FAILURE_RATE`, `KL_DIVERGENCE`. |
| `window_snapshot` | serialized RollingWindow (JSON) | Point-in-time capture for the LLM prompt input.            |

### DriftDiagnosis

Output of the LangGraph state machine. Persisted to SQLite and to Langfuse. Returned to the
caller via `GetDiagnosis` RPC.

| Field                     | Type                          | Constraints                                                                                  |
|---------------------------|-------------------------------|----------------------------------------------------------------------------------------------|
| `robot_id`                | string                        | Copied from `DriftEvent`.                                                                    |
| `fleet_action_id`         | string                        | The specific fleet_action_id that triggered the drift (typically the most recent sample).   |
| `window_key`              | 4-tuple                       | For dashboard grouping.                                                                     |
| `root_cause_hypotheses`   | list of string                | 1 to 5 items. Each ≤ 500 chars.                                                              |
| `recommended_action`      | enum                          | One of: `RETRAIN`, `RECALIBRATE_SENSOR`, `INSPECT_HARDWARE`, `INVESTIGATE`, `NO_ACTION`.     |
| `confidence`              | float                         | 0.0 ≤ value ≤ 1.0. LLM's confidence in the diagnosis itself.                                 |
| `human_readable_summary`  | string                        | ≤ 1000 chars. Written for a fleet operator.                                                  |
| `model_used`              | string                        | LLM identifier per LiteLLM (e.g., "anthropic/claude-3.5-sonnet").                            |
| `prompt_version`          | string                        | Version tag pulled from Langfuse prompt registry.                                            |
| `created_at`              | timestamp                     | UTC.                                                                                         |
| `langfuse_trace_id`       | string                        | Trace ID for post-hoc drill-down.                                                            |

### PromptVersion

Not stored in this project's data model. Lives in Langfuse. Referenced by name + version pin
in the sidecar `Config`.

### GuardrailBlock

Emitted when either LlamaGuard (input) or Guardrails AI (output) rejects. Persisted to Langfuse
(as an event on the trace) and reflected in Prometheus counters. NOT stored in the retrieval
SQLite (fail-closed errors are not "diagnoses").

| Field         | Type                     | Notes                                                                      |
|---------------|--------------------------|----------------------------------------------------------------------------|
| `check_type`  | enum                     | `INPUT_LLAMAGUARD` or `OUTPUT_GUARDRAILS_AI`.                              |
| `reason`      | string                   | Category (LlamaGuard) or validator name (Guardrails AI).                   |
| `payload_hash`| string (sha256 hex)      | Hash of the rejected payload for audit. Raw content NOT stored.            |
| `blocked_at`  | timestamp                | UTC.                                                                       |
| `correlation_ids` | robot_id + fleet_action_id | For post-hoc attribution when possible.                                |

### Config

Loaded at boot from Kubernetes ConfigMap + Secrets. Validated by Pydantic v2.

| Field                             | Type      | Default            | Notes                                                                 |
|-----------------------------------|-----------|--------------------|-----------------------------------------------------------------------|
| `active_model`                    | string    | `anthropic/claude-3.5-sonnet` | LiteLLM model identifier. Swap by config, not code.        |
| `max_samples`                     | int       | 200                | Rolling window count cap.                                             |
| `time_cap_hours`                  | int       | 24                 | Rolling window wall-clock cap.                                        |
| `min_sample_count`                | int       | 50                 | Below this, no drift checks.                                          |
| `sustained_samples`               | int       | 20                 | Consecutive samples the threshold must hold for drift to fire.        |
| `min_mean_confidence`             | float     | 0.85               | Below this in the last `sustained_samples`, drift fires.              |
| `max_failure_rate`                | float     | 0.15               | Failure rate above this, drift fires.                                 |
| `failure_confidence_threshold`    | float     | 0.5                | Sample counts as "failure" if confidence below this.                  |
| `max_kl_divergence`               | float     | 0.4                | KL(current || reference) above this, drift fires.                     |
| `snapshot_interval_seconds`       | int       | 60                 | RollingWindow snapshot cadence.                                       |
| `snapshot_interval_samples`       | int       | 20                 | Additional snapshot trigger on sample count.                          |
| `diagnosis_retention_days`        | int       | 30                 | SQLite pruning cadence.                                               |
| `dedup_retention_hours`           | int       | 24                 | Duplicate fleet_action_id rejection window.                           |
| `topk_class_predictions`          | int       | 5                  | Max class predictions carried per sample.                             |
| `langfuse_host`                   | string    | (required)         | Langfuse endpoint URL.                                                |
| `langfuse_prompt_name`            | string    | `drift-diagnosis`  | Registry lookup name.                                                 |
| `langfuse_prompt_version`         | string    | (required)         | Pinned version tag.                                                   |
| `litellm_host`                    | string    | (required)         | LiteLLM proxy endpoint URL.                                           |
| `llamaguard_model`                | string    | `meta-llama/LlamaGuard-3-8B` | LiteLLM identifier for the guardrail classifier.            |
| `sqlite_path`                     | string    | `/var/lib/sidecar/state.db` | Mount path for the PVC.                                     |
| `grpc_port`                       | int       | 50051              | gRPC listen port.                                                     |
| `metrics_port`                    | int       | 9090               | Prometheus scrape port.                                               |

Secrets (from Kubernetes Secrets, never from image or ConfigMap):
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
- `LITELLM_MASTER_KEY` (if LiteLLM proxy authentication is enabled)

## Relationships

- 1 `RollingWindow` ↔ many `TaskCompletionResult` (identity: 4-tuple key).
- 1 `RollingWindow` produces 0..1 `DriftEvent` per state entry into `drifting`.
- 1 `DriftEvent` produces exactly 1 `DriftDiagnosis` on the happy path; produces 1 or more `GuardrailBlock` events instead on the fail-closed path.
- 1 `DriftDiagnosis` retrievable by `(robot_id, fleet_action_id)`.

## Persistence Schema (SQLite)

```sql
CREATE TABLE IF NOT EXISTS diagnosis (
    robot_id           TEXT NOT NULL,
    fleet_action_id    TEXT NOT NULL,
    payload_json       TEXT NOT NULL,     -- serialized DriftDiagnosis
    created_at         TEXT NOT NULL,     -- ISO 8601 UTC
    prompt_version     TEXT NOT NULL,
    model_used         TEXT NOT NULL,
    langfuse_trace_id  TEXT NOT NULL,
    PRIMARY KEY (robot_id, fleet_action_id)
);

CREATE INDEX IF NOT EXISTS ix_diagnosis_created_at ON diagnosis(created_at);

CREATE TABLE IF NOT EXISTS window_snapshot (
    robot_id        TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    model_version   TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,        -- serialized RollingWindow state
    snapshotted_at  TEXT NOT NULL,
    PRIMARY KEY (robot_id, model_id, model_version, task_type)
);

CREATE TABLE IF NOT EXISTS dedup_seen (
    fleet_action_id  TEXT PRIMARY KEY,
    seen_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_dedup_seen_at ON dedup_seen(seen_at);
```

Pruning:
- `diagnosis` pruned by `created_at < now() - diagnosis_retention_days`.
- `window_snapshot` upserted in place; no pruning.
- `dedup_seen` pruned by `seen_at < now() - dedup_retention_hours`.
