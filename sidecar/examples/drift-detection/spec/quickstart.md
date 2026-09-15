# Quickstart Validation Guide

End-to-end validation that the drift-detection sidecar works. Not a code walkthrough.
Not a production runbook. Just: does the feature deliver the value the spec promises,
verifiably.

## Prerequisites

- Kubernetes cluster (kind, minikube, or a real cluster) at v1.27+.
- A running Langfuse instance reachable from the cluster. Prompt registry seeded
  with a prompt named `drift-diagnosis` at version `v1`.
- A running LiteLLM proxy reachable from the cluster. Configured with at least
  one LLM provider (Anthropic, OpenAI, or a local Ollama with Llama 3.x) and
  LlamaGuard available under an identifier LiteLLM recognizes.
- `kubectl` configured against the cluster.
- `grpcurl` on your workstation.

## Setup

1. Build the sidecar image and push to a registry the cluster can pull from.
2. Apply the Kubernetes manifests (Helm or Kustomize; see `deploy/`).
3. Create the required Secret:

   ```
   kubectl create secret generic sidecar-secrets \
     --from-literal=LANGFUSE_PUBLIC_KEY=... \
     --from-literal=LANGFUSE_SECRET_KEY=... \
     --from-literal=LITELLM_MASTER_KEY=...
   ```

4. Wait for the Deployment's pods to reach `Ready`. If they never do,
   read the pod logs: the sidecar is fail-closed on any missing dependency
   (Constitution Principle II) and will refuse to become ready if Langfuse
   or LiteLLM are unreachable.

## Validation Scenarios

### Scenario 1: Healthy stream produces no diagnoses

**Prove** that Story 1 acceptance scenario #2 holds.

1. Port-forward the sidecar's gRPC port locally.
2. Submit 300 `TaskCompletionResult` messages for the same
   `(robot_A, pick-model, 1.4.2, grip)` tuple with confidence values sampled
   from a normal distribution centered at 0.94, stddev 0.03.
3. Verify: no diagnosis appears in Langfuse. No `drift_alerts_total` metric
   increment. `GetDiagnosis` returns `NOT_FOUND` for every fleet_action_id.

Expected outcome: silence is correct. Sidecar does not fire false positives.

### Scenario 2: Confidence decay produces a diagnosis

**Prove** that Story 1 acceptance scenario #1 holds.

1. Continue submitting 200 more results for the same tuple with confidence
   values now sampled from mean 0.70, stddev 0.05 (below the default 0.85
   threshold).
2. Sustained-samples default is 20; drift should fire after ~20 sustained
   samples in the new regime.
3. Verify:
   - A new trace appears in Langfuse with the drift-diagnosis prompt.
   - `drift_alerts_total{robot_id=robot_A, model_id=pick-model, task_type=grip}` incremented by 1.
   - `GetDiagnosis(robot_A, <last submitted fleet_action_id>)` returns a
     `DriftDiagnosis` with root_cause_hypotheses, recommended_action,
     confidence, and human_readable_summary populated.
   - Round-trip latency (submission of triggering sample to diagnosis
     retrievable via `GetDiagnosis`) under 5 seconds p95 over 10 runs.

Expected outcome: drift caught, structured diagnosis produced, retrieval works.

### Scenario 3: Model swap via config only

**Prove** that Story 2 holds.

1. Note the `model_used` field in the diagnosis from Scenario 2 (should match
   the default, e.g. `anthropic/claude-3.5-sonnet`).
2. Edit the sidecar ConfigMap: set `active_model` to
   `openai/gpt-4o-mini` (or any other LiteLLM-supported id).
3. Trigger a rolling restart of the sidecar Deployment.
4. Repeat Scenario 2 with a fresh `(robot_id, fleet_action_id)` tuple.
5. Verify the new diagnosis's `model_used` field reflects the new choice.

Expected outcome: zero code changes required. Config swap is sufficient.

### Scenario 4: LiteLLM unreachable = fail-closed

**Prove** that Story 1 acceptance scenario #3 and Constitution Principle II hold.

1. Scale the LiteLLM Deployment to 0 replicas.
2. Repeat Scenario 2 (submit results that would trigger drift).
3. Verify:
   - The sidecar returns a gRPC error to callers on the diagnosis path (through
     any `Watch`-style interface if present, or on the internal state).
   - `guardrail_blocks_total` does NOT increment (this is a gateway failure,
     not a guardrail failure). A new metric `llm_gateway_failures_total`
     increments instead.
   - No diagnosis is persisted to SQLite. `GetDiagnosis` returns `NOT_FOUND`
     for the triggering `fleet_action_id`.
   - A structured event is written to Langfuse noting the gateway failure
     (assuming Langfuse itself is still up).

Expected outcome: no fabricated diagnosis. No fallback to "safe default". The
system fails visibly.

### Scenario 5: Malformed LLM output = fail-closed

**Prove** that Story 4 acceptance scenario #2 holds.

1. Configure LiteLLM to route requests to a stub that returns malformed JSON
   (missing required fields) for the drift-diagnosis prompt.
2. Repeat Scenario 2.
3. Verify:
   - `guardrail_blocks_total{check_type=OUTPUT_GUARDRAILS_AI}` incremented.
   - No diagnosis persisted to SQLite.
   - `GetDiagnosis` returns `NOT_FOUND`.
   - Langfuse trace shows the guardrail block event.

Expected outcome: fail-closed. Malformed output never surfaces.

### Scenario 6: Historical retrieval works after 24h

**Prove** that Story 3 holds and SC-002 is met.

1. From Scenario 2, note the `(robot_id, fleet_action_id)` of a produced diagnosis.
2. Wait 24 hours (or fast-forward the container's clock in a test).
3. Call `GetDiagnosis(robot_id, fleet_action_id)`.
4. Verify the diagnosis is returned identical to the original.

Expected outcome: durable retrieval works for at least 30 days.

## Cleanup

```
kubectl delete deployment sidecar
kubectl delete service sidecar
kubectl delete configmap sidecar-config
kubectl delete secret sidecar-secrets
kubectl delete pvc sidecar-state
```

## Success Bar

All six scenarios pass. Zero silent successes on any failure injection. Zero
diagnoses produced without a corresponding Langfuse trace. Every model-choice
swap achievable by config alone. Diagnoses retrievable within retention.
