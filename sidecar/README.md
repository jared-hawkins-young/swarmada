# swarmada-sidecar

The OSS LLM reasoning primitive for [Swarmada](../README.md).

LiteLLM proxy routing, Langfuse tracing, LangGraph orchestration, and
LlamaGuard + Guardrails AI safety layers, packaged as a small Python
library and a container image. Callers compose these primitives into
their own agents.

Everything is open source and self-hostable per [RFC-0002](../rfcs/RFC-0002-ai-sidecar.md)
and the project [Constitution](../.specify/memory/constitution.md).

## Stack

| Concern              | Component                                                                |
|----------------------|---------------------------------------------------------------------------|
| LLM routing / cost   | [LiteLLM](https://github.com/BerriAI/litellm) proxy (self-hosted)         |
| Tracing              | [Langfuse](https://github.com/langfuse/langfuse) (the OSS LangSmith)      |
| Orchestration        | [LangGraph](https://github.com/langchain-ai/langgraph) state machines     |
| Input safety         | [LlamaGuard](https://ai.meta.com/llama/) via LiteLLM route                |
| Output validation    | [Guardrails AI](https://github.com/guardrails-ai/guardrails) schema check |
| Metrics              | Prometheus (`/metrics` on port 9090)                                      |

## Architecture

```
                           +-----------------------------+
                           |   Downstream service        |
                           |   (gRPC / HTTP handlers)    |
                           +--------------+--------------+
                                          |
                                          v
                   +----------------------+----------------------+
                   |  swarmada_sidecar.graph.build_graph(...)    |
                   |    - AgentState + NodeContext               |
                   |    - Langfuse-nested spans per node         |
                   |    - fail-closed short-circuit routing      |
                   +--+--------+--------+--------+--------+------+
                      |        |        |        |        |
                      v        v        v        v        v
                +---------+ +-------+ +--------+ +------+ +---------+
                |Input    | | LLM   | | Output | |Store | |Domain   |
                |guardrail| |Gateway| |guardrl | |(caller|nodes    |
                |(Llama-  | |(Lite- | |(Guard- | |owned)| |(caller  |
                | Guard)  | | LLM)  | | rails) | |      | | wired)  |
                +---------+ +---+---+ +--------+ +------+ +---------+
                                |
                                v
                     +----------+----------+
                     | Langfuse (trace,    |
                     | generation, prompt  |
                     | registry)           |
                     +---------------------+
```

## Quickstart

```sh
# Install into a fresh 3.12 venv
uv pip install -e ".[dev]"

# Bring up your Langfuse + LiteLLM stack locally per docs/DEV.md.
# Then set the required env:
export SIDECAR_LANGFUSE_HOST=http://localhost:3000
export SIDECAR_LANGFUSE_PUBLIC_KEY=pk-...
export SIDECAR_LANGFUSE_SECRET_KEY=sk-...
export SIDECAR_LANGFUSE_PROMPT_VERSION=v1
export SIDECAR_LITELLM_HOST=http://localhost:4000
export SIDECAR_LITELLM_MASTER_KEY=sk-swarmada-local-master
export SIDECAR_ACTIVE_MODEL=local/llama3.2:1b
export SIDECAR_LLAMAGUARD_MODEL=local/llamaguard-mock
export SIDECAR_SQLITE_PATH=/tmp/swarmada-sidecar/state.db

# Seed a prompt into Langfuse:
python scripts/seed_prompt.py \
    --prompt-name drift-diagnosis \
    --file examples/drift-detection/prompts/drift-diagnosis.md \
    --version v1

# Run the foundation server (metrics + healthchecks only):
python -m swarmada_sidecar.server
```

The foundation itself exposes no gRPC surface. To see the primitives
composed into an end-to-end agent, run the drift-detection example:

```sh
cd examples/drift-detection
make proto-gen
python -m drift_detection
```

## Extending

Add a new LLM-using node by wiring the foundation primitives — never call
`litellm` / `openai` / `anthropic` SDKs directly. See
[docs/AGENT_PROTOCOL.md](./docs/AGENT_PROTOCOL.md) for the contract
(gateway usage, tracing conventions, prompt-registry rules).

For local dev setup (Langfuse compose, LiteLLM proxy, ollama), see
[docs/DEV.md](./docs/DEV.md).

## Reference implementation

[examples/drift-detection/](./examples/drift-detection/) — the original
robotics fleet drift-detection agent that motivated the foundation. It
uses every primitive: a rolling-window aggregator, a drift detector, a
LangGraph pipeline (`fetch_window → classify_drift → guardrail_input →
llm_diagnose → guardrail_output → persist → respond`), a gRPC
`SidecarService` at `sidecar.v1`, and a SQLite store for diagnoses.

## Design docs

- [rfcs/RFC-0002-ai-sidecar.md](../rfcs/RFC-0002-ai-sidecar.md) — the
  design that carved this subsystem out of Swarmada.
- [docs/AGENT_PROTOCOL.md](./docs/AGENT_PROTOCOL.md) — how to add an
  LLM-using node without breaking traceability or fail-closed posture.
- [docs/DEV.md](./docs/DEV.md) — local dev environment.
- [examples/drift-detection/spec/](./examples/drift-detection/spec/) —
  the historic spec for the reference implementation.
