# Implementation Plan: Model Drift Detection with AI Reasoning Sidecar

**Branch**: `001-drift-detection` | **Date**: 2026-09-14 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-drift-detection/spec.md`

## Summary

Deliver a Python microservice (the sidecar) that consumes structured task-completion results from the Swarmada Go control plane, aggregates them into per-(robot, model, task-type) rolling windows, detects drift against configurable thresholds, and invokes a LangGraph state machine that calls LiteLLM to produce a structured diagnosis. All LLM calls traced through Langfuse. All inputs and outputs guarded by LlamaGuard + Guardrails AI. All failure modes fail-closed. Deployed as a single-replica Kubernetes Deployment alongside the Swarmada control plane.

## Technical Context

**Language/Version**: Python 3.11+

**Primary Dependencies**:
- `grpcio` + `grpcio-tools` (gRPC contract with the Go control plane)
- `langgraph` (state-machine orchestration of the drift diagnosis workflow)
- `langfuse` Python SDK (trace middleware + prompt registry client)
- `litellm` (self-hosted model gateway, zero-markup)
- `llama-guard` weights via `transformers` (input safety classifier) OR routed through LiteLLM
- `guardrails-ai` (output structure validation)
- `pydantic` v2 (config + gRPC message models)
- `prometheus-client` (metrics)
- `sqlite3` (stdlib; embedded diagnosis + snapshot store)
- `uv` (dependency management; no poetry)

**Storage**: SQLite file mounted on a Kubernetes PersistentVolumeClaim. Two tables: `diagnosis` (retrieval by robot_id + fleet_action_id) and `window_snapshot` (crash-recovery for in-memory rolling windows).

**Testing**: `pytest` for unit + integration; `testcontainers-python` to spin up Langfuse and a LiteLLM stub during integration tests; `grpc_testing` for contract tests against the sidecar.v1 proto.

**Target Platform**: Linux containers on Kubernetes 1.27+. Distroless or Chainguard Python base image for the release build.

**Project Type**: Single Python microservice (gRPC server). Deployed via Helm and Kustomize surfaces.

**Performance Goals**:
- 5-second p95 drift-diagnosis latency end-to-end (SC-001).
- Sustained throughput: at least 100 task-completion submissions per second per replica (single-replica MVP).
- LLM call latency budget: under 3 seconds p95 (leaving 2 seconds for aggregation + guardrails + persistence + network).

**Constraints**:
- Zero un-traced LLM calls (Principle III, SC-004).
- Fail-closed on every guardrail, LiteLLM, prompt-registry, and dependency-health failure (Principle II, FR-010, SC-005, SC-006).
- All secrets from Kubernetes Secrets, none baked in image (FR-011).
- gRPC contract at `sidecar.v1` (Principle V, FR-013).
- No egress to public internet except to configured LLM provider endpoints via LiteLLM.

**Scale/Scope**:
- 1 to 100 robots per Swarmada control plane. Sidecar sized for up to ~50 unique (robot, model, task-type) rolling windows concurrently at MVP.
- Diagnosis history retention: 30 days (SC-002).
- Rolling window default: 200 samples or 24h wall clock, whichever fills first.

## Constitution Check

Gates derived from `.specify/memory/constitution.md` v1.0.0. Each MUST pass before Phase 0 research and MUST be re-checked after Phase 1 design.

| Gate | Principle | Verification |
|---|---|---|
| G1: Open-Source-First | I | Every listed dependency is OSI-licensed and self-hostable. LiteLLM (MIT), Langfuse (MIT), LangGraph (Apache 2.0), Guardrails AI (Apache 2.0), LlamaGuard (Meta open-weight), grpcio (Apache 2.0), pydantic (MIT), sqlite (public domain), prometheus-client (Apache 2.0). **PASS.** |
| G2: Fail-Closed by Default | II | Every external call in the LangGraph state machine (guardrail input, LLM, guardrail output, persist) surfaces errors to the caller; no branch silently returns a default diagnosis. Startup health check verifies Langfuse and LiteLLM reachable; pod refuses to become Ready otherwise. **PASS.** |
| G3: Traced By Default | III | Every LangGraph node is wrapped by the Langfuse trace middleware. Every LLM call goes through LiteLLM with the Langfuse callback attached. Prompts fetched by version from Langfuse prompt registry; no hardcoded strings. **PASS.** |
| G4: Spec-First | IV | This plan derives from spec.md, which was written before any implementation code. tasks.md and implementation follow, not lead. **PASS.** |
| G5: Contract Stability | V | Proto lives at `proto/sidecar/v1/sidecar.proto`. Package name `sidecar.v1`. Breaking changes will require `sidecar.v2` alongside `v1` support for a deprecation window. **PASS.** |
| G6: Author of Record | Constitution "Additional Constraints" | No AI-driven git commit or push. Human commits with DCO signoff. Verified by absence of any git operations in tasks.md. **PASS (documented, enforced in tasks).** |
| G7: Language + Runtime | Constitution "Additional Constraints" | Python 3.11+. uv (or pip-tools) for dependencies. No Poetry. Container base: Distroless or Chainguard. **PASS.** |

All gates pass. Proceed to Phase 0.

## Project Structure

### Documentation (this feature)

```text
specs/001-drift-detection/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output (proto + sample YAML)
├── checklists/
│   └── requirements.md  # Spec quality checklist
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
proto/
└── sidecar/
    └── v1/
        └── sidecar.proto              # gRPC contract (versioned per Principle V)

src/
└── sidecar/
    ├── __init__.py
    ├── server.py                      # gRPC server entrypoint, startup, health
    ├── config.py                      # Pydantic settings from ConfigMap + Secrets
    ├── aggregator.py                  # In-memory rolling window store + SQLite snapshots
    ├── drift_detector.py              # Threshold evaluator emitting DriftEvent
    ├── graph/
    │   ├── __init__.py
    │   ├── state.py                   # LangGraph state schema
    │   ├── nodes.py                   # fetch_window, classify_drift, guardrail_input,
    │   │                              # llm_diagnose, guardrail_output, persist, respond
    │   └── graph.py                   # Compiled StateGraph
    ├── gateway.py                     # LiteLLM wrapper; retry + timeout + trace attach
    ├── langfuse_client.py             # Trace middleware; prompt registry client
    ├── guardrails/
    │   ├── __init__.py
    │   ├── input_llamaguard.py        # LlamaGuard classifier wrapper
    │   └── output_guardrails_ai.py    # Guardrails AI schema validator
    ├── store.py                       # SQLite retrieval store for diagnoses + snapshots
    └── metrics.py                     # Prometheus counters, gauges, histograms

tests/
├── contract/
│   └── test_sidecar_v1_proto.py       # gRPC contract conformance
├── integration/
│   ├── conftest.py                    # testcontainers fixtures (Langfuse, LiteLLM stub)
│   ├── test_end_to_end_drift.py
│   ├── test_failclosed_langfuse_down.py
│   ├── test_failclosed_litellm_down.py
│   ├── test_failclosed_guardrail_input_block.py
│   ├── test_failclosed_guardrail_output_block.py
│   └── test_retrieval_by_correlation_ids.py
└── unit/
    ├── test_aggregator.py
    ├── test_drift_detector.py
    ├── test_guardrails_input.py
    ├── test_guardrails_output.py
    ├── test_gateway_retries.py
    └── test_store.py

deploy/
├── helm/
│   └── sidecar/
│       ├── Chart.yaml
│       ├── values.yaml
│       ├── templates/
│       │   ├── deployment.yaml
│       │   ├── service.yaml
│       │   ├── configmap.yaml
│       │   ├── pvc.yaml
│       │   ├── servicemonitor.yaml
│       │   └── _helpers.tpl
│       └── README.md
└── kustomize/
    ├── base/
    │   ├── kustomization.yaml
    │   ├── deployment.yaml
    │   ├── service.yaml
    │   ├── configmap.yaml
    │   ├── pvc.yaml
    │   └── servicemonitor.yaml
    └── overlays/
        └── example/
            └── kustomization.yaml

config/
├── sample-configmap.yaml              # All tunable knobs with defaults + comments
└── sample-secrets.example.yaml        # Documented but not deployed as-is

Dockerfile                             # Multi-stage; final on Chainguard python image
.dockerignore
pyproject.toml                         # uv-managed
uv.lock
Makefile                               # proto-gen, test, lint, build, docker
README.md                              # Repo-level readme
```

**Structure Decision**: Single Python microservice (Option 1 collapsed to a service shape). No frontend, no mobile. Deployment surfaces (Helm + Kustomize) live in `deploy/` alongside the source tree, per common Kubernetes-service conventions. Proto lives at `proto/sidecar/v1/` mirroring the Swarmada Go convention (see upstream `proto/fleet_adapter/v1/`).

## Complexity Tracking

No constitution violations. No complexity tracking required at this time.
