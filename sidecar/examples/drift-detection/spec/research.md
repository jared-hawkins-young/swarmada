# Phase 0 Research: Model Drift Detection with AI Reasoning Sidecar

Decisions in this doc were made during planning. No `NEEDS CLARIFICATION` markers remained
after Technical Context filled in. This artifact records the substantive design decisions,
their rationale, and the alternatives rejected.

## Decision 1: Model gateway = LiteLLM (self-hosted)

- **Decision**: Route all LLM calls through a self-hosted LiteLLM proxy running in-cluster.
- **Rationale**: Zero markup on token costs. Multi-provider (Anthropic, OpenAI, Google, HuggingFace, Ollama, local open-weight). Uniform OpenAI-compatible schema. Matches Principle I (Open-Source-First) and the constitution's requirement that model choice is a config change, not a code change.
- **Alternatives considered**:
  - **OpenRouter**: gateway-as-a-service, ~5% markup, no self-host path. Rejected on Principle I.
  - **Portkey**: bundles gateway + guardrails + observability. Rejected on tool overlap (Langfuse already picked for observability; Guardrails AI + LlamaGuard already picked for guardrails). Also thinner model catalog.
  - **Direct provider SDKs**: cheapest per call but forces vendor lock-in and breaks Principle I's swap-by-config requirement.

## Decision 2: LLM observability = Langfuse (self-hosted)

- **Decision**: Self-hosted Langfuse in-cluster. All LLM calls and LangGraph node transitions traced through Langfuse. Prompts stored in Langfuse prompt registry, version-pinned in config.
- **Rationale**: Open-source LangSmith. MIT license. Native LangGraph callback captures every node + tool + LLM call as a single nested trace. Prompt registry decouples prompt evolution from code deploys. Matches Principle III (Traced by Default).
- **Alternatives considered**:
  - **LangSmith**: proprietary, LangChain Inc. commercial service. Rejected on Principle I.
  - **OpenTelemetry alone**: covers spans and traces but not prompt registry or eval datasets. Would require additional tooling.
  - **Custom rollup on Prometheus**: no trace correlation, no prompt versioning. Insufficient.

## Decision 3: Orchestration = LangGraph

- **Decision**: LangGraph state machine for the drift-diagnosis workflow.
- **Rationale**: 2026 market data shows 44% production adoption, 81% satisfaction, 210% YoY growth over LangChain. Enterprise users include Klarna, Uber, LinkedIn, JPMorgan, BlackRock. Well-suited to stateful, branching, human-in-the-loop workflows with retries. Native Langfuse integration.
- **Alternatives considered**:
  - **CrewAI**: role-based multi-agent framework. Overkill for a single deterministic reasoning path.
  - **Straight Python function pipeline**: simplest, but loses first-class state, retries, and checkpointing. Would need to rebuild those.
  - **LangChain (base)**: still supported but the graph model is where new adoption is.

## Decision 4: Guardrails = LlamaGuard (input) + Guardrails AI (output)

- **Decision**: Two-layer guardrail stack. LlamaGuard classifies operator-provided text on inputs. Guardrails AI validates the LLM's structured JSON output against a Pydantic-defined schema.
- **Rationale**: Each tool serves a distinct job. LlamaGuard is a safety classifier (~1/3 the false positive rate of GPT-4 on Meta's benchmark). Guardrails AI enforces structured-output shape with 50+ validators. Both open-source and self-hostable, matching Principle I.
- **Alternatives considered**:
  - **NeMo Guardrails**: dialog-flow control via Colang DSL. Overkill because this feature has no conversational surface.
  - **LLM Guard alone**: fast first-layer scanner. Considered as a P2 addition; may layer in later.
  - **No guardrails**: rejected as retrofit pain later; wire them in from day one even though the current input surface is small.

## Decision 5: Local storage = SQLite (embedded)

- **Decision**: SQLite database file on a Kubernetes PVC. Two tables: `diagnosis` (retrieval by IDs) and `window_snapshot` (crash-recovery for rolling windows).
- **Rationale**: Single-replica MVP. No shared-state coordination requirement. SQLite gives durable persistence with zero operational overhead and no additional infrastructure component. Matches "least infra for the job."
- **Alternatives considered**:
  - **Postgres**: overkill at this scale, adds an infra dependency the sidecar doesn't need.
  - **In-memory only**: loses diagnosis history on pod restart, violates FR-008 (retrieval by IDs) and SC-002 (30-day retention).
  - **Object storage (S3 / MinIO)**: viable for cold archive; not fit for hot-path RPC retrieval latencies.
- **Follow-on**: if the sidecar later scales horizontally, migrate to Postgres or CockroachDB. Not required for MVP.

## Decision 6: Replica count = single-replica MVP

- **Decision**: Single-replica Kubernetes Deployment for MVP. Multi-replica deferred.
- **Rationale**: Rolling windows are in-memory. Snapshot-to-SQLite handles crash recovery. Diagnosis latency (5s p95) allows a straightforward single-replica implementation. Multi-replica introduces window-split, quorum, and consistency concerns that don't earn their complexity until throughput demands it.
- **Alternatives considered**:
  - **Multi-replica with sticky routing by robot_id**: doable but adds routing, snapshot merge, and startup replay complexity. Deferred.
  - **StatefulSet**: only necessary once we have persistent per-replica identity requirements. Not needed for single-replica.

## Decision 7: Rolling window = in-memory with periodic SQLite snapshots

- **Decision**: Rolling windows live in Python dicts keyed by (robot_id, model_id, model_version, task_type). Every N seconds (default 60) or every M new samples (default 20), snapshot to SQLite `window_snapshot` table. On startup, hydrate from the latest snapshots then continue.
- **Rationale**: Hot-path performance (adding a sample + computing running aggregates) stays in-process, sub-millisecond. Durability tolerance is measured in minutes, not seconds; a crash loses at most the most recent partial window since the last snapshot. Acceptable given rolling windows are inherently statistical.
- **Alternatives considered**:
  - **Every sample writes SQLite**: too slow. Wastes IOPS for zero real durability gain given the statistical nature of drift.
  - **No snapshots**: pod restart cold-starts every window, delaying drift detection until enough samples arrive. Bad UX.
  - **External state store (Redis, etc.)**: adds infra. Not justified for single-replica.

## Decision 8: Drift criteria = three configurable thresholds

- **Decision**: Drift fires when ANY of three conditions is true across a rolling window:
  1. Mean-confidence threshold: rolling mean drops below `min_mean_confidence` for the last `sustained_samples` events.
  2. Failure-rate threshold: rolling failure rate exceeds `max_failure_rate` for the last `sustained_samples` events.
  3. Class-distribution shift threshold: Kullback-Leibler divergence between the current window's class distribution and a reference distribution exceeds `max_kl_divergence`.
- **Rationale**: Each threshold catches a different failure mode. Mean confidence catches soft degradation. Failure rate catches hard degradation. KL divergence catches shifted priors (a new object class appears; the world changed).
- **Alternatives considered**:
  - **Single-threshold on confidence alone**: misses distribution shifts and hard failures where confidence stays high but the model is now wrong.
  - **Chi-square instead of KL**: comparable; KL chosen because it composes with information-theoretic reasoning in the LLM diagnosis prompt (LLM can be asked to explain "why does the class distribution have KL divergence 0.42 from baseline").
- **Follow-on**: reference distribution comes from either the window immediately after a new `ModelRollout` completes (bootstrap) or from a per-model calibration payload provided by the training pipeline. Bootstrap is the MVP default.

## Decision 9: gRPC surface = unary submit + unary retrieve

- **Decision**: Two RPCs on `sidecar.v1.SidecarService`:
  1. `SubmitTaskCompletion(TaskCompletionResult) -> SubmitAck` — unary. Client sends one result, server acknowledges.
  2. `GetDiagnosis(GetDiagnosisRequest) -> DiagnosisResponse` — unary. Client sends correlation IDs, server returns the diagnosis or a not-found status.
- **Rationale**: Task-completion arrival is naturally per-event; unary is simple and matches Swarmada's existing `ActionStatusUpdate` pattern. Streaming is not needed for the submission volume (≤100/s per replica).
- **Alternatives considered**:
  - **Bidi streaming**: unnecessary complexity. No back-channel needed on the submission path.
  - **Server-streaming for diagnoses**: viable for a "subscribe to drift events" client but out of scope for MVP; add later as `WatchDiagnoses`.

## Decision 10: LlamaGuard hosting = routed through LiteLLM initially

- **Decision**: Serve LlamaGuard via LiteLLM (either its Ollama backend or a hosted provider that lists LlamaGuard). Avoid bundling `transformers` + local weights into the sidecar image for MVP.
- **Rationale**: Keeps the sidecar image slim. Keeps model choice swappable (could substitute LLM Guard or a stricter classifier via config). LiteLLM already sits on the path; adding another model to its config surface is trivial.
- **Alternatives considered**:
  - **Bundle LlamaGuard weights + transformers into sidecar image**: image balloons to ~2-4 GB. Loading takes seconds. Rejected at MVP.
  - **External classifier service**: adds infra. Not justified.
- **Follow-on**: if latency at the guardrail step becomes a bottleneck, ship a small dedicated classifier deployment and swap by config.

## Decision 11: Prompt storage = Langfuse prompt registry, version-pinned

- **Decision**: The drift-diagnosis prompt lives in Langfuse prompt registry. Sidecar fetches by name + version pinned in ConfigMap. Prompt updates go through Langfuse UI; sidecar picks them up on config rollout, not on code redeploy.
- **Rationale**: Decouples prompt-tuning velocity from code-deploy cadence. Enables A/B on prompts against real drift data via Langfuse experiments. Matches Principle III.
- **Alternatives considered**:
  - **Hardcoded prompt in Python module**: fastest to write, but every prompt edit is a code change. Rejected.
  - **Prompt in ConfigMap**: better than hardcoded but loses Langfuse's prompt versioning, A/B, and eval dataset linkage.

## Decision 12: Test dependencies = testcontainers-python

- **Decision**: Use `testcontainers-python` to spin up ephemeral Langfuse and LiteLLM containers during integration tests. Unit tests keep zero infrastructure dependencies.
- **Rationale**: Real behavior against real services. Avoids mocking Langfuse SDK boundaries and getting a false green.
- **Alternatives considered**:
  - **Full mock/stub**: fragile against SDK version updates. False confidence.
  - **Shared long-running test env**: harder to reset, slower, cross-test pollution risk.

## Open Follow-ons (not blocking Phase 1)

- Reference distribution bootstrap policy for KL divergence: MVP uses the first N=500 samples after a `ModelRollout` completes; refine once real deployments give us data.
- Multi-replica story: revisit once single-replica bottlenecks are quantified.
- Grafana dashboard shipped as an `examples/` artifact (not a supported surface).
- Later phase: `WatchDiagnoses` streaming RPC for subscribers.
