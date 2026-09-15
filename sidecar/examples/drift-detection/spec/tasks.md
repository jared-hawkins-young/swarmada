---

description: "Tasks for feature 001 — Model Drift Detection with AI Reasoning Sidecar"
---

# Tasks: Model Drift Detection with AI Reasoning Sidecar

**Input**: Design documents from `/specs/001-drift-detection/`

**Prerequisites**: plan.md, spec.md (user stories), research.md, data-model.md, contracts/, quickstart.md, constitution v1.0.0.

**Tests**: PIVOT (2026-09-14 evening) — pre-written pytest tests have been REMOVED from
the repo. Testing model is now: (1) structured logs on every hot path, (2) real end-to-end
runs against the live stack as the primary correctness check, (3) ad-hoc AI-written unit
tests as gaps surface. Rationale: the `tests/{contract,integration}` suite passed 61/61
while 7 real integration bugs (SDK contract mismatches, wrong health endpoint, missing
proxy routing, un-wired traces) survived — because in-process fakes stand in for the
network, the SDK, and the proxy. See
`/Users/jaredhawkins-young/Life/Career/open-source/Swarmada/tasks/current/2026-09-14_real-e2e-run-findings.md`.
Old tests backed up under
`/private/tmp/claude-501/.../scratchpad/deleted-tests-backup/` this session in case
individual pieces (e.g. schema validators) prove worth reviving as narrow ad-hoc tests.

Task IDs T016-T023, T034-T037, T044-T046, T051-T052, T073-T074 below (all "write a test"
tasks) are struck through as no longer part of the plan. Constitution Principle II
(Fail-Closed) is now enforced by the real end-to-end run, not by upfront tests.

**Organization**: Tasks grouped by user story so each story is independently deliverable.
Story dependencies noted where they exist. Within each story, order is:
proto → schema → aggregator → drift_detector → LangGraph nodes → gateway → guardrails
→ server → tests → deployment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no in-flight deps)
- **[Story]**: US1, US2, US3, US4 (maps to spec.md priorities)
- Every task includes a concrete file path.

## Path Conventions

Single Python microservice at repository root. Source under `src/sidecar/`, tests under `tests/`, proto under `proto/`, deploy under `deploy/`, configs under `config/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: repo bootstrap and dev-environment scaffolding.

- [x] T001 Create project skeleton per plan.md structure at `/Users/jaredhawkins-young/code/swarmada-ai-sidecar/` (empty `src/sidecar/`, `tests/{unit,integration,contract}/`, `proto/sidecar/v1/`, `deploy/{helm,kustomize}/`, `config/`)
- [x] T002 Initialize `pyproject.toml` with `uv`; declare `grpcio`, `grpcio-tools`, `langgraph`, `langfuse`, `litellm`, `guardrails-ai`, `pydantic>=2`, `prometheus-client`, `pytest`, `pytest-asyncio`, `testcontainers`, dev tools (`ruff`, `mypy`) at `pyproject.toml`
- [x] T003 [P] Configure `ruff.toml` for lint + format at `ruff.toml`
- [x] T004 [P] Configure `mypy.ini` with strict typing at `mypy.ini`
- [x] T005 [P] Create `.dockerignore` and multi-stage `Dockerfile` (Chainguard python base for the release stage) at `Dockerfile` and `.dockerignore`
- [x] T006 [P] Create `Makefile` with targets `proto-gen`, `test`, `lint`, `build`, `docker` at `Makefile`
- [x] T007 [P] Create repository-level `README.md` pointing at the spec artifacts at `README.md`
- [x] T008 Copy `specs/001-drift-detection/contracts/sidecar.v1.proto` to `proto/sidecar/v1/sidecar.proto` (canonical location for build)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: core wiring every user story depends on. MUST be complete before ANY story starts.

- [x] T009 Run `make proto-gen` and commit generated Python stubs at `src/sidecar/v1/*.py{,i}` (regenerable; not hand-edited). Note: `make proto-gen` outputs to `src/sidecar/v1/` (not `src/sidecar/proto/sidecar/v1/`); the imports in `server.py` and `SidecarServicer` reflect that.
- [x] T010 Implement `Config` in `src/sidecar/config.py` — Pydantic v2 settings loaded from env (Kubernetes ConfigMap projection) + Secret refs. Include every field enumerated in `data-model.md`.
- [x] T011 Implement Prometheus metric registration in `src/sidecar/metrics.py` with counters `drift_alerts_total`, `guardrail_blocks_total`, `llm_gateway_failures_total`, histograms `llm_call_latency_seconds`, `submission_latency_seconds`, gauges `rolling_confidence`, `rolling_failure_rate`, `rolling_kl_divergence`, `dependency_healthy` (per dep)
- [x] T012 Implement SQLite store bootstrap in `src/sidecar/store.py` — create the `diagnosis`, `window_snapshot`, `dedup_seen` tables per `data-model.md` schema, with the specified indexes
- [x] T013 Implement Langfuse client wrapper in `src/sidecar/langfuse_client.py` — trace-span middleware, prompt-registry fetch by name+version, health-check helper. FAIL-CLOSED on unreachable.
- [x] T014 Implement LiteLLM gateway wrapper in `src/sidecar/gateway.py` — model selection by config, retry with exponential backoff (max 3), per-call timeout (default 3s), Langfuse callback attached automatically. FAIL-CLOSED on retry exhaustion.
- [x] T015 Implement startup healthcheck in `src/sidecar/server.py` — verify Langfuse reachable, LiteLLM reachable, prompt-registry entry present, SQLite writable. Refuse to enter Ready state on any failure (FR-010).

**Checkpoint**: infrastructure ready. Every user story can now start in parallel.

---

## Phase 3: User Story 1 — Automatic drift diagnosis on a degraded robot (P1) 🎯 MVP

**Goal**: given a stream of task-completion results, the sidecar detects drift and produces a structured, human-readable diagnosis end-to-end within 5s p95, traced through Langfuse.

**Independent Test**: feed 200 synthetic results with mean confidence dropping below threshold; verify diagnosis appears in `GetDiagnosis` and in Langfuse, all Prometheus counters increment correctly.

### Tests for User Story 1 (write first; must FAIL before implementation)

- [x] T016 [P] [US1] Contract test for `SubmitTaskCompletion` RPC in `tests/contract/test_submit_task_completion.py`
- [x] T017 [P] [US1] Contract test for `GetDiagnosis` RPC in `tests/contract/test_get_diagnosis.py`
- [x] T018 [P] [US1] Unit test for `RollingWindow` aggregation math in `tests/unit/test_aggregator.py` (mean confidence, failure count, KL divergence, sample eviction on count + time cap)
- [x] T019 [P] [US1] Unit test for `DriftDetector` threshold logic in `tests/unit/test_drift_detector.py` (each of the 3 criteria, sustain condition, bootstrap gate)
- [x] T020 [P] [US1] Unit test for `Store.insert_diagnosis / get_diagnosis / prune` in `tests/unit/test_store.py`
- [x] T021 [P] [US1] Integration test happy-path drift → diagnosis in `tests/integration/test_end_to_end_drift.py`. Note: uses in-process `FakeLangfuseClient` + `FakeGateway` from `tests/conftest.py` so it runs without Docker. Add a testcontainers Langfuse + LiteLLM stub variant later for higher fidelity.
- [x] T022 [P] [US1] Integration test fail-closed on LiteLLM unreachable in `tests/integration/test_failclosed_litellm_down.py` (startup + runtime paths)
- [x] T023 [P] [US1] Integration test fail-closed on Langfuse unreachable at startup in `tests/integration/test_failclosed_langfuse_down.py` (auth failure + missing pinned prompt)

### Implementation for User Story 1

- [x] T024 [US1] Implement `RollingWindow` and `AggregatorStore` in `src/sidecar/aggregator.py` — in-memory dict keyed by 4-tuple, running aggregates for mean confidence + failure count + class histogram + KL, snapshot to SQLite `window_snapshot` on interval (T seconds or N samples), hydrate on startup from most recent snapshots
- [x] T025 [US1] Implement `DriftDetector` in `src/sidecar/drift_detector.py` — evaluates a RollingWindow against the 3 configured thresholds, respects `min_sample_count` bootstrap gate and `sustained_samples` requirement, emits `DriftEvent`. FAIL-CLOSED: if reference distribution unavailable for KL, do NOT bypass; skip only that check and log
- [x] T026 [US1] Implement LangGraph state schema in `src/sidecar/graph/state.py` — Pydantic model for the state carried across nodes (drift_event, guardrail_verdicts, llm_response, diagnosis, errors)
- [x] T027 [US1] Implement LangGraph nodes in `src/sidecar/graph/nodes.py` — `fetch_window`, `classify_drift`, `guardrail_input`, `llm_diagnose`, `guardrail_output`, `persist`, `respond`. Each node wrapped by Langfuse trace-span middleware.
- [x] T028 [US1] Implement `Guardrails/Input (LlamaGuard)` wrapper in `src/sidecar/guardrails/input_llamaguard.py` — routes through LiteLLM by config, returns `SAFE | UNSAFE(reason)`, emits `guardrail_blocks_total` on UNSAFE
- [x] T029 [US1] Implement `Guardrails/Output (Guardrails AI)` wrapper in `src/sidecar/guardrails/output_guardrails_ai.py` — validates the LLM response against the `DriftDiagnosis` Pydantic schema; returns `VALID | INVALID(reason)`; emits `guardrail_blocks_total` on INVALID
- [x] T030 [US1] Implement compiled StateGraph in `src/sidecar/graph/graph.py` — wires the nodes with fail-closed edges: any node error terminates the graph and returns an explicit error (never a fabricated diagnosis)
- [x] T031 [US1] Implement `SubmitTaskCompletion` RPC handler in `src/sidecar/server.py` — validates the incoming message against `data-model.md` constraints, dedups by `fleet_action_id` (retention window), invokes `AggregatorStore.add_sample`, if the resulting window fires drift, dispatches the LangGraph state machine async
- [x] T032 [US1] Implement `GetDiagnosis` RPC handler in `src/sidecar/server.py` — reads from `Store`, returns `NOT_FOUND` when absent (not an empty response)
- [x] T033 [US1] Populate default `drift-diagnosis` prompt (v1) into Langfuse prompt registry via a helper script at `scripts/seed_prompt.py` — includes structured-output schema instructions matching the `DriftDiagnosis` message
- [x] T034 [US1] Fail-closed test — malformed LLM JSON output blocks and does NOT persist diagnosis in `tests/integration/test_failclosed_guardrail_output_block.py` (missing field, unparseable JSON, out-of-range confidence)
- [x] T035 [US1] Fail-closed test — LlamaGuard rejects operator-provided metadata blocks and does NOT reach LiteLLM in `tests/integration/test_failclosed_guardrail_input_block.py` (asserts gateway.calls == [] when input is UNSAFE)
- [ ] T036 [US1] DCO commit reminder (HUMAN, not AI): stage the completed Phase 3 slice; commit with `git commit -s -m "feat(us1): drift detection MVP end to end"`. NOTE: `src/` currently has 51 ruff findings from earlier phases; either fix those first or split the commit — new test files under `tests/{conftest.py,contract/,integration/}` are lint- and format-clean.

**Checkpoint**: User Story 1 is functional end-to-end. MVP complete.

---

## Phase 4: User Story 2 — Config-only LLM swap (P2)

**Goal**: developer swaps active model by ConfigMap edit + pod restart; zero code change; new model reflected in Langfuse traces.

**Independent Test**: swap `active_model` from Claude to GPT-4 to Llama via ConfigMap, restart pod, replay drift; Langfuse traces show each `model_used` correctly.

### Tests for User Story 2

- [x] T037 [P] [US2] Unit test — invalid `active_model` value fails config validation at startup (fail-closed) in `tests/unit/test_config_validation.py`
- [ ] T038 [P] [US2] Integration test — same drift replay with three different `active_model` values produces three different `model_used` fields in the persisted diagnoses in `tests/integration/test_model_swap.py`

### Implementation for User Story 2

- [ ] T039 [US2] Extend `Config` to include supported-provider allowlist and pattern-validate `active_model` at startup in `src/sidecar/config.py`
- [ ] T040 [US2] Ensure `Gateway` re-reads config on process start only (no hot reload for MVP; simplicity) in `src/sidecar/gateway.py`
- [ ] T041 [US2] Extend `LangfuseClient` to include `model_used` metadata on every trace span in `src/sidecar/langfuse_client.py`
- [ ] T042 [US2] Emit `model_used` into the persisted `DriftDiagnosis` record via `Store.insert_diagnosis` in `src/sidecar/store.py`
- [ ] T043 [US2] DCO commit reminder (HUMAN): `git commit -s -m "feat(us2): config-driven model swap"`

**Checkpoint**: User Story 2 is functional.

---

## Phase 5: User Story 3 — Retrieval by correlation IDs (P2)

**Goal**: any diagnosis can be retrieved by `(robot_id, fleet_action_id)` for at least 30 days.

**Independent Test**: produce a diagnosis, note IDs, wait (or clock-fast-forward), retrieve, compare against original.

### Tests for User Story 3

- [ ] T044 [P] [US3] Integration test — retrieval returns identical payload in `tests/integration/test_retrieval_by_correlation_ids.py`
- [ ] T045 [P] [US3] Integration test — retrieval returns `NOT_FOUND` gRPC status for absent tuples in `tests/integration/test_retrieval_not_found.py`
- [ ] T046 [P] [US3] Unit test — retention pruning drops rows past `diagnosis_retention_days` in `tests/unit/test_store_pruning.py`

### Implementation for User Story 3

- [ ] T047 [US3] Implement background pruning task in `src/sidecar/store.py` — runs every N minutes, prunes `diagnosis` rows older than `diagnosis_retention_days`, prunes `dedup_seen` older than `dedup_retention_hours`
- [ ] T048 [US3] Wire the pruning task into `server.py` startup as a background asyncio task
- [ ] T049 [US3] Add Prometheus counter `diagnoses_pruned_total` and gauge `diagnosis_row_count` in `src/sidecar/metrics.py`
- [ ] T050 [US3] DCO commit reminder (HUMAN): `git commit -s -m "feat(us3): diagnosis retention + retrieval"`

**Checkpoint**: User Story 3 is functional.

---

## Phase 6: User Story 4 — Guardrail plumbing hardened (P3)

**Goal**: guardrail wiring in place from day one so future operator-input surfaces cannot bypass filtering.

**Independent Test**: feed adversarial metadata to `SubmitTaskCompletion`; verify LlamaGuard rejects before LiteLLM is called; verify malformed LLM output is rejected by Guardrails AI.

### Tests for User Story 4

- [ ] T051 [P] [US4] Integration test — LlamaGuard blocks adversarial input categories in `tests/integration/test_llamaguard_categories.py`
- [ ] T052 [P] [US4] Integration test — Guardrails AI schema failure surfaces as fail-closed error, not a partial diagnosis in `tests/integration/test_guardrails_ai_schema_fail.py`

### Implementation for User Story 4

- [ ] T053 [US4] Wire `guardrail_input` and `guardrail_output` explicitly into the LangGraph edges so no code path can bypass either in `src/sidecar/graph/graph.py`
- [ ] T054 [US4] Emit `guardrail_blocks_total{check_type,reason}` for every block in `src/sidecar/metrics.py`
- [ ] T055 [US4] Write structured Langfuse events on every guardrail block in `src/sidecar/guardrails/{input_llamaguard.py,output_guardrails_ai.py}`
- [ ] T056 [US4] DCO commit reminder (HUMAN): `git commit -s -m "feat(us4): guardrail plumbing hardened"`

**Checkpoint**: User Story 4 is functional.

---

## Phase 7: Deployment Surfaces

**Purpose**: ship the sidecar as a deployable Kubernetes workload. NOT gated on any single user story; can proceed in parallel with US2-US4 once US1 lands.

- [ ] T057 [P] Helm chart Chart.yaml + values.yaml at `deploy/helm/sidecar/{Chart.yaml,values.yaml}`
- [ ] T058 [P] Helm template `deployment.yaml` with liveness + readiness probes wired to the sidecar's `/healthz` at `deploy/helm/sidecar/templates/deployment.yaml`
- [ ] T059 [P] Helm template `service.yaml` at `deploy/helm/sidecar/templates/service.yaml`
- [ ] T060 [P] Helm template `configmap.yaml` sourcing every non-secret field of `Config` at `deploy/helm/sidecar/templates/configmap.yaml`
- [ ] T061 [P] Helm template `pvc.yaml` for the SQLite mount at `deploy/helm/sidecar/templates/pvc.yaml`
- [ ] T062 [P] Helm template `servicemonitor.yaml` for Prometheus scrape at `deploy/helm/sidecar/templates/servicemonitor.yaml`
- [ ] T063 [P] Helm helpers `_helpers.tpl` and chart-level README at `deploy/helm/sidecar/{templates/_helpers.tpl,README.md}`
- [ ] T064 [P] Kustomize base under `deploy/kustomize/base/{kustomization.yaml,deployment.yaml,service.yaml,configmap.yaml,pvc.yaml,servicemonitor.yaml}`
- [ ] T065 [P] Kustomize example overlay under `deploy/kustomize/overlays/example/kustomization.yaml`
- [ ] T066 [P] Sample ConfigMap YAML documenting every tunable knob (defaults + comments) at `config/sample-configmap.yaml`
- [ ] T067 [P] Documented (never applied as-is) sample Secret example at `config/sample-secrets.example.yaml`
- [ ] T068 DCO commit reminder (HUMAN): `git commit -s -m "chore: helm + kustomize deployment surfaces"`

---

## Phase 8: Polish & Cross-Cutting

**Purpose**: fill in the gaps that don't belong to any single user story.

- [ ] T069 Grafana example dashboard JSON at `examples/dashboards/drift-detection.json` — panels for drift alerts over time, per-robot rolling confidence, LLM latency distribution, guardrail block rate
- [ ] T070 [P] Repo-level `CONTRIBUTING.md` at `CONTRIBUTING.md` — references the Constitution, spec-first workflow, DCO signoff requirement
- [ ] T071 [P] Repo-level `SECURITY.md` at `SECURITY.md` — vuln reporting policy
- [ ] T072 [P] Repo-level `LICENSE` at `LICENSE` (Apache 2.0 to match Swarmada culture)
- [ ] T073 Extend integration test suite with the 6 quickstart scenarios from `specs/001-drift-detection/quickstart.md` as automated acceptance tests in `tests/integration/test_quickstart_scenarios.py`
- [ ] T074 Add p95 latency regression test in `tests/integration/test_latency_p95.py` verifying SC-001 target
- [ ] T075 Wire the sidecar to expose Prometheus `/metrics` on the `metrics_port` in `src/sidecar/server.py`
- [ ] T076 Wire `/healthz` and `/readyz` HTTP endpoints reflecting startup healthcheck state in `src/sidecar/server.py`
- [ ] T077 DCO commit reminder (HUMAN): `git commit -s -m "chore: dashboards, docs, license, latency + quickstart tests"`

---

## Dependencies

- **Phase 1 (Setup)** blocks everything.
- **Phase 2 (Foundational)** blocks Phase 3 through 6. Deployment surfaces (Phase 7) can start once T009 (proto stubs) exists.
- **Phase 3 (US1)** is the MVP. Phases 4, 5, 6 add value but are independent of each other; they can run in parallel once Phase 3 lands.
- **Phase 7 (Deployment)** is parallel-safe after Foundational.
- **Phase 8 (Polish)** is last, uses artifacts from all prior phases.

Story-level dependencies:
- US2 (config-driven model swap) depends only on the LiteLLM gateway wiring completed in US1.
- US3 (retrieval) depends on the SQLite store from Foundational + insert path from US1.
- US4 (guardrail hardening) refines wiring already introduced in US1; no new external component.

## Parallelizable Slices

- All tasks marked `[P]` run in parallel within their phase.
- Once Foundational is done, US1 tests (T016-T023) run in parallel.
- US1 implementation tasks are largely sequential inside the LangGraph (T024 → T025 → T026 → T027) but T028 and T029 (guardrails) run in parallel with T024-T027.
- Phase 7 (Helm + Kustomize) is mostly file-parallel except the DCO commit at the end.

## Implementation Strategy

**MVP scope** = User Story 1 (Phase 1 + Phase 2 + Phase 3). Ship it as the first internal release. Everything after (US2, US3, US4, deployment surfaces, polish) is layered on incrementally without breaking US1.

**Time estimate (single developer, part-time)**:
- Phase 1 + 2: 1 week
- Phase 3 (MVP): 2 weeks
- Phase 4 + 5 + 6: 1 week combined
- Phase 7 (deploy): 3-5 days
- Phase 8 (polish): 2-3 days

Total: ~5-6 weeks of focused work for a shippable v0.1 with 100% test coverage on fail-closed paths.

## Constitutional Alignment

Every task above is consistent with Constitution v1.0.0:
- **Principle I (Open-Source-First)**: every dependency introduced in tasks is OSS + self-hostable.
- **Principle II (Fail-Closed by Default)**: explicit fail-closed tests (T022, T023, T034, T035, T037, T045, T052) and fail-closed edges in T030, T053.
- **Principle III (Traced By Default)**: every LangGraph node wrapped in Langfuse middleware (T027); no un-traced LLM call.
- **Principle IV (Spec-First)**: these tasks derive from the spec + plan + research + data-model + contracts, in that order.
- **Principle V (Contract Stability)**: proto at `sidecar.v1` from T008; regeneration at T009; no ad-hoc contract changes without proto version bump.
- **Additional constraint (Author of Record)**: every phase ends with an explicit DCO commit task (T036, T043, T050, T056, T068, T077); no AI-driven commits.
- **Additional constraint (Language + Runtime)**: uv-managed Python 3.11+ from T002; Chainguard base from T005.
