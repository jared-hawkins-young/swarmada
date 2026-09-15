# RFC-0002 — AI Reasoning Sidecar

- **Status:** Draft
- **Author:** @jared-hawkins-young
- **Superseded by:** —
- **Related:** [RFC-0001 core spec](RFC-0001-overview.md), [ADR-0006 north side is the Kubernetes API](../docs/adr/0006-north-side-is-the-kubernetes-api.md)

## 1. Summary

Add a new subsystem to Swarmada — a Python microservice (the
"sidecar") — that provides an OSS-first, self-hostable LLM reasoning
layer for the fleet and is the **mandatory chokepoint** for every LLM
call anywhere in the monorepo. Every LLM call goes through one
model-routing proxy (LiteLLM) and one observability layer (Langfuse),
composed with a multi-step orchestration primitive (LangGraph) and two
guardrails (LlamaGuard on input, Guardrails AI on output).

The sidecar exposes:
- A **gRPC surface** (`sidecar.gateway.v1.LLMGateway/Complete`) that
  any Swarmada component — Go controllers, Python simulation, external
  tooling — calls when it needs an LLM. Language-agnostic.
- A **Python SDK** (`swarmada_sidecar.gateway.Gateway.call`) for
  in-process Python callers.

The rule is enforced by a repo-root CI check
(`.github/workflows/no-direct-llm-sdk.yml`) that grep-scans the whole
monorepo for direct imports of LLM SDKs (`litellm`, `openai`,
`anthropic`, `langchain`, `google.generativeai`, etc.) and fails the
build if any exist outside `sidecar/src/swarmada_sidecar/gateway.py`.
So the "mandatory" is not a social convention — it's a build gate.

The sidecar is **not currently invoked by any existing controller**
because upstream Swarmada Go has zero LLM calls today. When any future
capability wants one — starting with the drift-detection reference
example under `sidecar/examples/` — it uses the sidecar. It cannot
route around it.

The scope of this RFC is **the primitive plus the enforcement**, not a
specific use case. Drift diagnosis on a degraded robot is one worked
example — provided as a reference implementation under
`sidecar/examples/drift-detection/` — but the point is that every
future capability that needs LLM reasoning stands on the same primitive
without reinventing observability, safety, or model plumbing, and
cannot bypass it.

## 2. Motivation

Robot fleet operators increasingly ask questions that don't have a
closed-form answer in the control plane's existing CRDs: "why did this
robot's pick success rate drop overnight," "which of these three
adapters is misbehaving," "summarize this task's failure across the
fleet." Answering these consistently requires an LLM. Doing it
consistently and safely requires:

- **Model routing** — the ability to swap providers (Anthropic, OpenAI,
  local Ollama, Together, HuggingFace endpoints) by ConfigMap edit
  rather than by code change, and to compare providers on the same
  workload.
- **Tracing** — every LLM call captured with provenance (input, output,
  model, token counts, cost, latency, correlation IDs), so an operator
  or auditor can trace a diagnosis back to the exact prompt and model
  version that produced it.
- **Guardrails** — input safety classification (LlamaGuard) and
  structured-output validation (Guardrails AI) on the same call, so
  operator-provided text can never bypass safety and malformed model
  output can never propagate a partial diagnosis.
- **Orchestration** — multi-step agent flows (fetch context → classify
  → call model → validate → persist) with fail-closed short-circuiting.

Building these one-off inside each future feature would fragment the
observability story, split cost accounting, and let each feature
re-implement safety differently. A single primitive fixes that.

## 3. Non-goals

- **Not on-robot inference.** The sidecar reasons over telemetry the
  control plane already has. Vision inference stays on the robot. The
  sidecar consumes structured results (confidences, class predictions,
  task-completion tuples), not raw sensor frames.
- **Not a replacement for existing controllers.** Nothing in `api/v1`
  or the reconcile loops changes. The sidecar is invoked from
  controllers that opt in via configuration.
- **Not vendor-locked.** Every dependency (LiteLLM, Langfuse,
  LangGraph, LlamaGuard, Guardrails AI) is Apache-2.0 or MIT and
  self-hostable. LiteLLM covers routing to hosted providers when an
  operator chooses that; the primitive itself never requires them.
- **Not synchronous on the hot path.** No control-plane reconcile
  waits on an LLM. All sidecar calls are async or out-of-band; no LLM
  latency ever appears in a Robot status write, a FleetAction
  transition, or an adapter dispatch.
- **Not a training pipeline.** The sidecar consumes models via the
  routing proxy. Training and fine-tuning are out of scope.

## 4. Proposed architecture

### 4.1 Component

A new top-level directory `sidecar/` containing a Python 3.12
microservice. The sidecar builds a container image and deploys as a
Kubernetes workload (Helm chart lands in a follow-up commit under
`deploy/helm/sidecar/`). It exposes:

- **gRPC** on port `SIDECAR_GRPC_PORT` (default 50051) — the mandatory
  `sidecar.gateway.v1.LLMGateway/Complete` RPC that every Swarmada
  component calls when it needs an LLM. Contract at
  `sidecar/proto/gateway/v1/gateway.proto`. Response includes the
  Langfuse `trace_id` so callers can persist it alongside their own
  state for downstream correlation.
- **HTTP** on port `SIDECAR_METRICS_PORT` (default 9099) — `/metrics`
  (Prometheus), `/healthz` (liveness), `/readyz` (readiness).

The service refuses to enter `Ready` unless every dependency
(LiteLLM proxy, Langfuse, SQLite state, pinned prompt registry
entry) is reachable — fail-closed per Principle II below.

### 4.1a CI enforcement of the "single chokepoint" rule

`.github/workflows/no-direct-llm-sdk.yml` runs on every push and PR. It
grep-scans the entire monorepo for banned imports:

```
^(from|import) +(litellm|openai|anthropic|langchain|
                 google\.generativeai|cohere|mistralai|together)( |\.|$)
```

and fails the build if any occur outside
`sidecar/src/swarmada_sidecar/gateway.py`. Excludes: `vendor/`,
`.venv/`, `node_modules/`, `__pycache__/`, `proto_gen/`, `.git/`. The
same check runs locally as `make check-router-only` from `sidecar/`.

This makes the "one chokepoint" property a build gate, not a
convention. A contributor who adds `import openai` in a Go adapter's
sidecar Python helper, or in a simulation script, or in a controller
test, cannot merge without either routing through the sidecar or
explicitly justifying an exception in a follow-up RFC.

### 4.2 Foundation modules

- `sidecar.gateway.Gateway` — every LLM call goes through
  `Gateway.call(...)`. Wraps LiteLLM's Python client, targets a
  self-hosted LiteLLM proxy over HTTP with `custom_llm_provider="openai"`
  routing. Auto-registers Langfuse's `langfuse_otel` success + failure
  callback so every completion emits a nested `generation` observation
  with model, tokens, and cost. Exponential-backoff retries with a
  configurable per-call timeout. Retry exhaustion raises
  `LiteLLMGatewayError` (fail-closed).
- `sidecar.langfuse_client.LangfuseClient` — thin adapter around
  Langfuse SDK 4.x (OpenTelemetry-based). `root_pipeline(...)` is a
  context manager that opens one root chain span per invocation and
  yields the trace_id. `trace_span(...)` is a decorator that wraps
  a callable in a nested span (as generation/span/tool/guardrail).
- `sidecar.graph` — LangGraph scaffolding. `AgentState` (Pydantic;
  carries an errors list, a langfuse_trace_id, and a free-form
  payload dict), `NodeContext` (dependency injection for gateway +
  langfuse + guardrails + store), and `_bind(fn, ctx, name)` that
  wraps every LangGraph node in a Langfuse span automatically.
- `sidecar.guardrails.InputGuardrail` — LlamaGuard-backed safety
  classifier (routed through LiteLLM by config; any LlamaGuard-shaped
  backend works).
- `sidecar.guardrails.OutputGuardrail` — generic Guardrails AI wrapper
  that validates an LLM's JSON response against a caller-supplied
  Pydantic model. Fail-closed on any schema violation.

### 4.3 Config-driven model swap

`SIDECAR_ACTIVE_MODEL` is the ConfigMap-injected env var that picks the
drift-diagnosis model. Swapping models is a ConfigMap edit + pod
restart — no code change. The **catalog** of models the proxy knows
about is managed in the LiteLLM proxy UI (issue keys, add backends,
set per-key cost caps); the sidecar's config just names one route
from that catalog.

### 4.4 Reference example: drift detection

`sidecar/examples/drift-detection/` contains a working end-to-end
implementation: aggregate task-completion samples into rolling windows
per (robot, model, task_type); when configured thresholds trip, run a
LangGraph pipeline that fetches the window, checks input safety,
diagnoses via the configured LLM, validates the structured JSON, and
persists the diagnosis for retrieval by correlation ID. It shows the
end-to-end shape without dictating that every future capability follow
the same pattern.

## 5. Safety invariants

- **RA-1 (status-write discipline) is preserved.** The sidecar writes
  no Robot status. Any capability that surfaces sidecar output to a CRD
  status field does so through its own controller reconciler, not
  in-line during a telemetry tick.
- **Guardrails are always in the path.** Every LLM call is preceded by
  LlamaGuard on any operator-provided input and validated by
  Guardrails AI on output. Bypass is a bug.
- **Fail-closed on every external dependency.** Langfuse unreachable at
  startup ⇒ pod refuses `Ready`. LiteLLM retry exhausted ⇒ pipeline
  short-circuits, no fabricated diagnosis. LLM returns malformed JSON
  ⇒ output guardrail blocks, nothing persists.
- **Never on the hot path.** Sidecar work is async or out-of-band; no
  LLM latency ever appears in a reconcile loop or an adapter dispatch.

## 6. Configuration

A new `SwarmadaConfig.spec.ai` block (added in a subsequent RFC when
we wire the Go side) will express: which sidecar service to reach,
which model to use for which capability, per-capability cost caps, and
opt-in flags per FleetAction / ModelPolicy that gate whether AI
reasoning is applied. RFC-0002 does NOT change any CRD; that comes in
RFC-0003 alongside the Go-side wiring.

## 7. Observability

- Every trace lands in Langfuse. Root span is
  `<pipeline_name>_pipeline`; nested spans per LangGraph node; nested
  `generation` observation per LiteLLM call with model + tokens + cost.
- Prometheus metrics (foundation set): `llm_call_latency_seconds`,
  `llm_gateway_failures_total`, `guardrail_blocks_total`,
  `dependency_healthy`. Example-specific metrics live under the
  example (drift-detection adds `drift_alerts_total`, rolling
  gauges, etc.).
- Correlation IDs: every persisted output includes the Langfuse
  `trace_id` for round-trip inspection.

## 8. Migration and rollout

- Ships behind a feature flag; defaults off.
- No existing CRD is modified in this RFC.
- The drift-detection reference example is optional and does not run
  unless deployed.
- Removal is a config change; the sidecar leaves no persistent
  Kubernetes state beyond its own PVC (SQLite for example state) and
  its own trace history in Langfuse.

## 9. Alternatives considered

- **On-robot LLM inference.** Rejected: robot compute budgets can't
  support this consistently, and centralizing gives us traceability and
  cost accounting per fleet-wide decision.
- **A single hosted vendor (LangSmith / OpenAI-only).** Rejected:
  violates Principle I (open-source-first) and forces every operator
  onto a specific SaaS.
- **Rolling our own routing + tracing.** Rejected as reinvention.
  LiteLLM and Langfuse are the OSS reference implementations for these
  two problems and cover the model catalog and observability needs
  without lock-in.
- **A Go implementation.** Rejected: the LLM ecosystem (LangGraph,
  Guardrails AI, LiteLLM native) is Python-first. A separate Python
  sidecar preserves the Go core's simplicity and lets Python evolve
  faster in the AI space.

## 10. Open questions

- **RPC surface shape.** Should the sidecar expose one generic
  `Complete(prompt, model, metadata) → response` RPC, or
  capability-specific RPCs per use case (like the drift example's
  `SubmitTaskCompletion` / `GetDiagnosis`)? Current lean: both — the
  generic RPC for callers that want raw model access with tracing
  and safety, capability-specific RPCs added per use case for
  richer contracts. RFC-0003 will decide.
- **Cost accounting for local inference.** LiteLLM reports `cost=0`
  for `local/*` and `ollama/*` routes because there's no pricing row.
  Should we attribute self-hosted compute cost (GPU-time × utilization)
  via a post-processing job, or leave local as free?
- **Prompt versioning + rollout.** Langfuse's prompt registry supports
  labels and numeric versions. What's the rollout policy — pin to a
  version per environment, or promote a `production` label?
- **Multi-tenancy.** Multiple Swarmada tenants sharing one sidecar
  needs project-scoped Langfuse writes and per-tenant cost caps. Out
  of scope for RFC-0002; called out for RFC-0003.

## 11. Adoption plan

1. Land this RFC + the sidecar scaffold on a branch of the fork
   (`feat/ai-sidecar-scaffold`). No CRD changes, no Go-side wiring.
2. Cut a draft PR against upstream `main` after maintainer review of
   this RFC.
3. Follow-up RFC-0003 defines the `SwarmadaConfig.spec.ai` CRD block
   and the Go-side client wiring (which controllers may call the
   sidecar, how opt-in is expressed per FleetAction/ModelPolicy).
4. First operator-facing capability lands in RFC-0004: AI-verified
   task completion (safety-gated, opt-in per FleetAction) built on
   the drift-detection reference example's shape.
