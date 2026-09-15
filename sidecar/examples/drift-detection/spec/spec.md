# Feature Specification: Model Drift Detection with AI Reasoning Sidecar

**Feature Branch**: `001-drift-detection`

**Created**: 2026-09-14

**Status**: Draft

**Input**: User description: The Swarmada fleet orchestrator deploys AI models to robots but has no fleet-scale reasoning about how those models perform once running. This feature adds a Python microservice (the sidecar) that consumes structured task-completion results, detects drift, and produces human-readable diagnoses with recommended actions.

## User Scenarios & Testing

### User Story 1 - Fleet operator gets automatic drift diagnosis on a degraded robot (Priority: P1)

A fleet operator manages a warehouse of robots picking items. One robot's picking success rate has been quietly degrading over the last 24 hours from 94% mean confidence to 78%, though the model version hasn't changed. Today the operator only notices when a downstream metric (throughput) drops or a customer complains. With this feature, the sidecar detects the drift automatically, produces a structured diagnosis (probable causes, recommended action, human-readable summary), and surfaces it to the operator's Grafana dashboard and to the Swarmada control plane within minutes.

**Why this priority**: This is the whole point of the sidecar. Every other story derives from this working. Without automatic drift detection, the sidecar is a wrapper around nothing.

**Independent Test**: Can be tested by feeding the sidecar a synthetic stream of 200 structured task-completion results whose rolling mean confidence drops below the configured threshold. The sidecar must produce a diagnosis and emit it to the caller and to Langfuse. Value delivered: a real drift signal that would otherwise go unnoticed.

**Acceptance Scenarios**:

1. **Given** a rolling window of 200 completed tasks for (robot_A, model_pick_v1.4.2, task_type=grip), **When** the mean confidence in that window falls below the configured threshold of 0.85 and stays there for the last 20 tasks, **Then** the sidecar emits a drift diagnosis containing root_cause_hypotheses, a recommended_action, an overall confidence score, and a human_readable_summary, within 5 seconds p95 of the triggering event.
2. **Given** a healthy rolling window whose mean confidence stays above threshold, **When** new task-completion results arrive, **Then** the sidecar continues to aggregate but produces no diagnosis (silence is the correct answer).
3. **Given** the LLM provider is unreachable, **When** a drift event fires, **Then** the sidecar returns a fail-closed error to the caller and does NOT produce a fabricated diagnosis or a "safe default" verdict.

---

### User Story 2 - Developer swaps the LLM behind the diagnosis without a code redeploy (Priority: P2)

A developer wants to compare Claude, GPT-4, and an open-weight Llama model for drift diagnosis quality on the same production drift events. Today, swapping models means changing code and shipping a release. With this feature, the model choice lives in configuration; changing which LLM is called requires only a config update and a pod restart. Langfuse traces let the developer compare outputs across models on the same rolling windows.

**Why this priority**: A/B model comparison is the primary way this feature earns Principle I (Open-Source-First) in practice. Without configurable model routing, we're just hardcoded to whichever LLM we picked first.

**Independent Test**: Deploy the sidecar with `active_model: anthropic/claude-3.5-sonnet` configured, trigger a drift event, capture the Langfuse trace. Change config to `active_model: openai/gpt-4o`, restart, replay, capture second trace. Compare in Langfuse. No source code changed. Value delivered: real-world evaluation flexibility.

**Acceptance Scenarios**:

1. **Given** the sidecar running with `active_model` set to a supported provider, **When** the operator edits the ConfigMap and restarts the pod, **Then** subsequent drift events call the new provider and Langfuse traces show the new model_name field.
2. **Given** a config specifying a model that is not in the supported provider list, **When** the pod starts, **Then** startup fails with a clear configuration error (fail-closed).

---

### User Story 3 - Historical drift diagnoses are retrievable by correlation ID (Priority: P2)

A fleet operator investigating a past incident wants to retrieve the drift diagnosis that fired for a specific robot on a specific action. Today, if the diagnosis wasn't captured to a durable store, it's gone. With this feature, every diagnosis is written to Langfuse (traceable by correlation IDs) AND the Swarmada control plane can query the sidecar by (robot_id, fleet_action_id) tuple to retrieve the full diagnosis at any time.

**Why this priority**: Post-incident debugging is table stakes for anything shipped into a robotics fleet. Without retrieval, diagnoses are ephemeral.

**Independent Test**: Trigger a drift event that produces a diagnosis, note the (robot_id, fleet_action_id). Wait 24 hours. Query the sidecar's retrieval API with the same tuple. Retrieve the exact diagnosis payload. Value delivered: post-hoc audit capability.

**Acceptance Scenarios**:

1. **Given** a drift diagnosis was produced with correlation IDs (robot_A, action_123), **When** the Go control plane calls the retrieval RPC with those IDs, **Then** the sidecar returns the original diagnosis payload including timestamps and the LLM model used.
2. **Given** a retrieval request for a (robot_id, fleet_action_id) tuple with no associated diagnosis, **When** the RPC is called, **Then** the sidecar returns a not-found response, NOT an empty diagnosis or fabricated response.

---

### User Story 4 - Guardrail block on adversarial operator input (Priority: P3)

A future extension of the sidecar (out of scope for this feature but on the roadmap) will accept free-text operator queries. To prepare for that, the guardrail plumbing (LlamaGuard on inputs, Guardrails AI on outputs) is wired in from day one. Any operator-provided text that passes through the sidecar today (e.g., diagnosis-request metadata, custom tags) is filtered. Malicious or unsafe text is blocked.

**Why this priority**: Building guardrails in from the start prevents retrofit pain. Actual operator-input paths are P1 in a future feature; this story validates that the guardrail plumbing exists and refuses to allow bypasses.

**Independent Test**: Feed the sidecar a diagnosis-request with metadata text known to trip LlamaGuard's unsafe categories. Verify the request is rejected before any LLM call is made. Value delivered: safe-by-construction posture.

**Acceptance Scenarios**:

1. **Given** an incoming diagnosis-request whose metadata contains text classified unsafe by LlamaGuard, **When** the request enters the sidecar, **Then** the request is rejected with a guardrail-block error before any LLM is called.
2. **Given** the LLM produces JSON that fails the Guardrails AI output schema, **When** the sidecar receives the LLM response, **Then** the sidecar returns a fail-closed error, does NOT emit the malformed diagnosis, and records the block in Langfuse and Prometheus.

---

### Edge Cases

- What happens when the rolling window has fewer than the minimum sample count (default 50)? Sidecar aggregates silently, does not trigger drift analysis, does not emit a diagnosis.
- What happens when two different task types share the same (robot_id, model_id, model_version) but only one is drifting? Each (robot_id, model_id, model_version, task_type) tuple has its own independent rolling window and drift decision.
- What happens when a robot goes offline mid-window? The window retains prior samples; new samples resume when the robot comes back. Drift analysis triggers on samples, not on wall-clock time alone.
- What happens when the Swarmada control plane sends a duplicate task-completion result (same fleet_action_id)? The sidecar deduplicates by fleet_action_id within a configurable retention window. Duplicate events are logged but do not skew rolling metrics.
- What happens when a task-completion result arrives with a model_id or model_version the sidecar has never seen? A new window is created; the event is aggregated normally. Model registry catches up implicitly.
- What happens when the LiteLLM gateway returns a partial or truncated response? The Guardrails AI output schema will catch missing required fields; sidecar returns fail-closed error.
- What happens when Langfuse is unavailable? The sidecar cannot start (Principle III: no un-traced inference). Startup healthcheck fails; the pod does not enter Ready state.

## Requirements

### Functional Requirements

- **FR-001**: System MUST accept structured task-completion results from the Swarmada control plane via a versioned gRPC RPC, carrying at minimum the fields: robot_id, fleet_action_id, model_id, model_version, confidence, class_predictions (optional), captured_at, task_type.
- **FR-002**: System MUST maintain a rolling window of the most recent N results per unique (robot_id, model_id, model_version, task_type) tuple, where N and the time-based cap are configurable (defaults: N=200, time_cap=24h).
- **FR-003**: System MUST evaluate each rolling window for drift signals against three configurable criteria: mean-confidence threshold, failure-rate threshold, and class-distribution shift threshold.
- **FR-004**: System MUST NOT emit a diagnosis when the window is below the minimum sample count (default 50) even if the window's metrics appear to indicate drift.
- **FR-005**: When drift is detected, System MUST fetch a versioned prompt from the Langfuse prompt registry and call the configured LLM via LiteLLM.
- **FR-006**: System MUST enforce guardrails on inputs (LlamaGuard) and on the LLM's JSON output (Guardrails AI). Guardrail failures MUST return a fail-closed error to the caller with no fallback diagnosis.
- **FR-007**: System MUST persist every produced diagnosis to Langfuse with correlation IDs for robot_id and fleet_action_id, and to a local durable store keyed by (robot_id, fleet_action_id) for later retrieval.
- **FR-008**: System MUST expose a retrieval RPC that accepts (robot_id, fleet_action_id) and returns the associated diagnosis or a not-found response.
- **FR-009**: System MUST expose Prometheus metrics for at minimum: drift_alerts_total (counter, labeled by robot_id/model_id/task_type), llm_call_latency_seconds (histogram, labeled by model), guardrail_blocks_total (counter, labeled by check_type/reason), rolling_confidence (gauge, labeled by robot_id/model_id/task_type).
- **FR-010**: System MUST fail to start if any required upstream dependency (Langfuse, LiteLLM gateway, configured LLM provider) is unreachable at boot, per Principle II (Fail-Closed).
- **FR-011**: System MUST accept configuration via ConfigMap and secrets via Kubernetes Secrets, and MUST NOT read secrets from environment variables baked into container images.
- **FR-012**: System MUST allow the choice of active LLM to be changed by config update and pod restart, without code changes, per Principle I (Open-Source-First supports model swap).
- **FR-013**: System MUST version its gRPC contract using semver at the proto package level, per Principle V (Contract Stability). Initial version: `sidecar.v1`.
- **FR-014**: System MUST deduplicate incoming task-completion results by fleet_action_id within a configurable retention window (default 24h).

### Key Entities

- **TaskCompletionResult**: A structured record of one task attempt from one robot. Attributes: robot_id, fleet_action_id (unique), model_id, model_version (semver), confidence (0-1), class_predictions (optional top-k list), captured_at (timestamp), task_type.
- **RollingWindow**: The bounded sliding buffer of TaskCompletionResults for a unique (robot_id, model_id, model_version, task_type) tuple. Bounded by count (N) and by wall-clock age (time_cap). Carries current aggregates (mean confidence, failure rate, class distribution).
- **DriftEvent**: Emitted when a RollingWindow crosses configured thresholds. Carries the window snapshot at trigger time and the reason (which threshold was crossed).
- **DriftDiagnosis**: Produced by the LangGraph state machine in response to a DriftEvent. Structured JSON with root_cause_hypotheses (list of strings), recommended_action (enum: retrain | recalibrate_sensor | inspect_hardware | investigate), confidence (0-1), human_readable_summary (string), model_used (string, from LiteLLM), prompt_version (string, from Langfuse), created_at (timestamp).
- **PromptVersion**: A versioned prompt template stored in the Langfuse prompt registry. Pinned to a specific version by config; not hardcoded.
- **GuardrailBlock**: A structured record of an input or output that failed a guardrail check. Carries reason (LlamaGuard category or Guardrails AI validator name), the offending payload (with sensitive content redacted), and timestamp. Emitted to both Langfuse and Prometheus.

## Success Criteria

### Measurable Outcomes

- **SC-001**: A drift event that meets configured thresholds produces a diagnosis end-to-end within 5 seconds at the 95th percentile.
- **SC-002**: Fleet operators can retrieve any drift diagnosis by (robot_id, fleet_action_id) tuple within 30 days of its creation.
- **SC-003**: The choice of active LLM can be changed by config update alone. Zero source code modifications required to swap between Claude, GPT-4, and any LiteLLM-supported open-weight model.
- **SC-004**: Zero drift diagnoses produced without a corresponding Langfuse trace. Reconciliation check between diagnoses persisted locally and traces in Langfuse must show 100% coverage.
- **SC-005**: Zero drift diagnoses returned when either the input guardrail or the output guardrail failed. Every guardrail failure results in an explicit fail-closed error to the caller.
- **SC-006**: When the sidecar's dependencies (Langfuse, LiteLLM, configured LLM provider) are unreachable, the pod does not enter Ready state. Kubernetes health checks reflect this correctly.
- **SC-007**: A rolling window with mean confidence sustained above threshold for 24 hours produces zero diagnoses. False-positive rate at the drift-detection layer is under 1%.
- **SC-008**: Fleet operators reviewing a diagnosis can, from the human_readable_summary alone, identify the affected robot, the affected model, and at least one concrete next action. Measured via 5-operator usability review of 20 real diagnoses; at least 90% of diagnoses meet this bar.

## Assumptions

- Upstream training pipeline produces new model versions via existing Swarmada ModelPolicy webhook flow. This sidecar does not train, retrain, or fine-tune models.
- The Swarmada Go control plane will extend the existing `ActionStatusUpdate` message (or add a new message) to carry the structured task-completion fields required by FR-001. That upstream change is coordinated separately as part of the RFC to Swarmada.
- Robots are already emitting on-board vision-model confidence for their actions. The presence and format of these fields on the adapter side is a Swarmada upstream contract, not this project's responsibility.
- Langfuse is deployed in the same cluster (or reachable from it) with sufficient retention for at least 30 days of diagnosis traces.
- LiteLLM is deployed in the same cluster with credentials for the configured LLM providers.
- Kubernetes cluster is version 1.27 or newer, supports the CRDs Swarmada uses, and allows egress to the LLM provider endpoints via LiteLLM.
- Fleet operators access diagnoses through a Grafana dashboard fed by Prometheus + Langfuse. The dashboard itself is out of scope for this feature (delivered as an example Grafana JSON, but not a supported product surface).
- The sidecar deploys as a single Kubernetes Deployment plus Service, not as a StatefulSet, because rolling windows are in-memory + periodically snapshotted to a durable store; single-replica is acceptable for MVP.

## Dependencies

- Swarmada upstream repository (github.com/swarmada/swarmada). RFC required to add the task-completion result structured fields to the Fleet Adapter protocol.
- Langfuse (open-source, self-hosted). Deployed in the same cluster with prompt registry populated.
- LiteLLM (open-source, self-hosted). Deployed in the same cluster with provider keys mounted as Kubernetes Secrets.
- LlamaGuard model weights (Meta open-weight). Runs inside the sidecar as a local classifier or via LiteLLM.
- Guardrails AI Python package. Installed via `uv`.
- Prometheus scrape target configured for the sidecar's `/metrics` endpoint.
- Constitution v1.0.0 principles (all five) apply to every implementation decision.
