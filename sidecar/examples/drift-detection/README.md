# drift-detection

Reference implementation demonstrating the [`swarmada-sidecar`](../../README.md)
foundation primitives end-to-end.

Takes structured task-completion samples from a fleet of autonomous
robots over gRPC, aggregates them into rolling per-(robot, model,
version, task_type) windows, evaluates configurable drift thresholds
(mean confidence, failure rate, KL divergence), and — on trip — runs a
LangGraph-orchestrated LLM diagnosis pipeline that emits a structured
`DriftDiagnosis`. Every LLM call is proxied through LiteLLM and traced
in Langfuse.

## Pipeline

```
SubmitTaskCompletion(gRPC)
    -> AggregatorStore.add_sample
    -> DriftDetector.evaluate
        (trip)
    -> LangfuseClient.root_pipeline("drift_diagnosis_pipeline")
        -> fetch_window
        -> classify_drift
        -> guardrail_input      (LlamaGuard via LiteLLM)
        -> llm_diagnose         (active_model via LiteLLM)
        -> guardrail_output     (Guardrails AI schema check)
        -> persist              (SQLite via DriftStore)
        -> respond              (emit pipeline latency histogram)
```

Any error in any node short-circuits to `respond` (fail-closed); the
drift is still recorded but `GetDiagnosis` returns `NOT_FOUND` for that
`(robot_id, fleet_action_id)`.

## Foundation primitives used

| Foundation                                              | How this agent uses it                             |
|---------------------------------------------------------|----------------------------------------------------|
| `swarmada_sidecar.config.Config`                        | Model routing, Langfuse creds, LiteLLM host, ports |
| `swarmada_sidecar.gateway.Gateway`                      | Every LLM call (LlamaGuard + diagnosis)            |
| `swarmada_sidecar.langfuse_client.LangfuseClient`       | Root pipeline trace + prompt registry              |
| `swarmada_sidecar.graph.state.AgentState`               | State carrier (payload dict for intermediate data) |
| `swarmada_sidecar.graph.graph.build_graph`              | Compiles the 7-node pipeline with fail-closed routing |
| `swarmada_sidecar.guardrails.input_llamaguard`          | Input safety on operator-facing drift event JSON   |
| `swarmada_sidecar.guardrails.output_guardrails_ai`      | Validates LLM output against `DriftDiagnosisPayload` |
| `swarmada_sidecar.metrics.*`                            | LLM latency, gateway failures, guardrail blocks    |

The drift-specific pieces layered on top:

- `drift_config.DriftConfig` — thresholds and retention windows.
- `drift_metrics.*` — `sidecar_drift_alerts_total`, rolling gauges, etc.
- `aggregator.py` — rolling window store + `TaskSample`.
- `drift_detector.py` — three-check threshold evaluator.
- `store.py` — `DriftStore` (diagnoses, snapshots, dedup).
- `graph_nodes.py` — the 7 domain nodes + `DriftDiagnosisPayload` schema.
- `server_ext.py` — `DriftSidecarServicer` for the gRPC surface.
- `__main__.py` — startup wiring.

## Run it locally

Prereqs — bring up Langfuse, LiteLLM, and ollama per
[../../docs/DEV.md](../../docs/DEV.md), then:

```sh
# From swarmada/sidecar/examples/drift-detection/

# Install (also editable-installs the sibling foundation)
uv pip install -e ".[dev]"

# Generate the Python gRPC stubs from proto/
make proto-gen

# Seed the prompt into Langfuse
python ../../scripts/seed_prompt.py \
    --prompt-name drift-diagnosis \
    --file prompts/drift-diagnosis.md \
    --version v1

# Set env (see ../../docs/DEV.md for the full list) and run
export SIDECAR_LANGFUSE_HOST=http://localhost:3000
export SIDECAR_LANGFUSE_PUBLIC_KEY=pk-lf-...
export SIDECAR_LANGFUSE_SECRET_KEY=sk-lf-...
export SIDECAR_LANGFUSE_PROMPT_VERSION=v1
export SIDECAR_LITELLM_HOST=http://localhost:4000
export SIDECAR_LITELLM_MASTER_KEY=sk-swarmada-local-master
export SIDECAR_ACTIVE_MODEL=local/llama3.2:1b
export SIDECAR_LLAMAGUARD_MODEL=local/llamaguard-mock
export SIDECAR_SQLITE_PATH=/tmp/drift-detection/state.db

python -m drift_detection
```

Fire a `SubmitTaskCompletion` at `localhost:50051` (grpcurl works;
generate a client from `proto/sidecar/v1/sidecar.proto`). Repeat until
you cross the threshold, then watch the Langfuse UI at
`http://localhost:3000` — one trace named `drift_diagnosis_pipeline` per
trip, with nested spans per node and a nested `generation` observation
for the diagnosis LLM call.

## Historic spec

The original Spec Kit spec chain for this feature lives under
[spec/](./spec/) — the artifact that motivated the foundation split.
`spec.md` is the requirements doc; `plan.md`, `data-model.md`,
`quickstart.md`, `research.md`, and `tasks.md` are its supporting
artifacts. The proto `.proto` was copy of `contracts/sidecar.v1.proto`
frozen at implementation time.
