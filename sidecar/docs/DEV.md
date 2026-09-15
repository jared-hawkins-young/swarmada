# Local dev environment

End-to-end local stack for developing `swarmada-sidecar` and its examples.
Everything is OSS and runs on one laptop.

## Prerequisites

- Docker (or Colima) with `docker compose`.
- Python 3.12+ and `uv` (`brew install uv` on macOS).
- [ollama](https://ollama.com/) for local LLM serving.
- `make`.

## 1. Langfuse

Bring up the official Langfuse v3/v4 Docker Compose stack from the
[langfuse/langfuse](https://github.com/langfuse/langfuse) repo:

```sh
git clone https://github.com/langfuse/langfuse ~/code/langfuse
cd ~/code/langfuse
docker compose up -d
```

Wait for the web UI at http://localhost:3000, then:

1. Create an org + project through the UI.
2. Generate a project API key pair (public + secret).
3. Export them for the sidecar:

   ```sh
   export SIDECAR_LANGFUSE_HOST=http://localhost:3000
   export SIDECAR_LANGFUSE_PUBLIC_KEY=pk-lf-...
   export SIDECAR_LANGFUSE_SECRET_KEY=sk-lf-...
   ```

### Langfuse v4 dual-write gotcha

If Langfuse's compose is on the v4 line, its public API defaults to
`events_only` mode and rejects reads. Set the environment variable
`LANGFUSE_MIGRATION_V4_WRITE_MODE=dual` on the `langfuse-web` container
(add it to the compose override or `.env`). Without this, every
`langfuse.get_prompt(...)` call returns an `"events_only mode"` error
and startup fails at gate #3.

## 2. ollama

```sh
ollama serve                       # keeps a local server running
ollama pull llama3.2:1b            # small, fast, good enough for graph-shape testing
```

### ollama wedge / reset

ollama occasionally deadlocks on model download interruptions or GPU
context switches. Symptoms: `ollama list` hangs, or LiteLLM proxy calls
time out even though `curl http://localhost:11434/api/tags` returns.

Reset:

```sh
pkill -9 ollama
rm -rf ~/.ollama/models/.lock ~/.ollama/models/manifests/*/incomplete 2>/dev/null || true
ollama serve
```

If the wedge persists, `rm -rf ~/.ollama` and re-pull the model. Local
dev only — you never want to do this in prod.

## 3. LiteLLM proxy

```sh
cp sidecar/config/litellm-local.yaml.example sidecar/config/litellm-local.yaml
# fill in the {{...}} placeholders

pipx install "litellm[proxy]"
export LITELLM_MASTER_KEY=sk-swarmada-local-master
export LANGFUSE_HOST=$SIDECAR_LANGFUSE_HOST
export LANGFUSE_PUBLIC_KEY=$SIDECAR_LANGFUSE_PUBLIC_KEY
export LANGFUSE_SECRET_KEY=$SIDECAR_LANGFUSE_SECRET_KEY

litellm --config sidecar/config/litellm-local.yaml --port 4000
```

Confirm the proxy is up:

```sh
curl http://localhost:4000/health/liveness
```

## 4. Seed a prompt into Langfuse

```sh
cd sidecar
python scripts/seed_prompt.py \
    --prompt-name drift-diagnosis \
    --file examples/drift-detection/prompts/drift-diagnosis.md \
    --version v1
```

## 5. Run the foundation

```sh
export SIDECAR_LITELLM_HOST=http://localhost:4000
export SIDECAR_LITELLM_MASTER_KEY=sk-swarmada-local-master
export SIDECAR_ACTIVE_MODEL=local/llama3.2:1b
export SIDECAR_LLAMAGUARD_MODEL=local/llamaguard-mock
export SIDECAR_LANGFUSE_PROMPT_VERSION=v1
export SIDECAR_LANGFUSE_PROMPT_NAME=drift-diagnosis
export SIDECAR_SQLITE_PATH=/tmp/swarmada-sidecar/state.db

cd sidecar
uv pip install -e ".[dev]"
python -m swarmada_sidecar.server
```

`curl http://localhost:9090/readyz` should return `ready` once startup
finishes (Langfuse reachable, prompt fetched, LiteLLM reachable).

## 6. Run the drift-detection example

```sh
cd sidecar/examples/drift-detection
uv pip install -e .
make proto-gen
python -m drift_detection
```

That starts the drift-detection gRPC server on `SIDECAR_GRPC_PORT`
(default 50051). Fire a `SubmitTaskCompletion` at it, watch Langfuse for
the pipeline trace with nested generation observations, and query
`GetDiagnosis` to read the persisted output.

## 7. Swap to a real provider (Anthropic, OpenAI, Gemini, OpenRouter…)

Everything above uses local ollama so you can run without an API bill.
To route through a real hosted provider — anywhere from one-off testing
to production — the setup is **three edits, no code change:**

1. **Export the provider's API key** in the shell that starts the
   LiteLLM proxy:

   ```sh
   export ANTHROPIC_API_KEY=sk-ant-...
   # or export OPENAI_API_KEY=sk-...
   # or export GEMINI_API_KEY=...
   # or export OPENROUTER_API_KEY=...
   ```

   LiteLLM reads `*_API_KEY` env vars per provider. You do NOT put the
   key in any file.

2. **Uncomment the matching route** in `config/litellm-local.yaml`
   (see the "Hosted providers" section in the `.example` template). No
   restart of the sidecar needed — just LiteLLM.

3. **Point the sidecar at the new route** by setting
   `SIDECAR_ACTIVE_MODEL` to the route name (e.g.
   `anthropic/claude-3-5-sonnet`), then restart the sidecar. Config
   change; no code change.

That's it. In Langfuse the next completion will show:

- `model` = the route name you enabled (e.g.
  `anthropic/claude-3-5-sonnet`)
- `cost` = a real number in USD (populated by LiteLLM's built-in
  `model_prices.json`; local/* routes report $0 because no external
  bill)
- `usage` = real provider-reported token counts

### Adding a provider that LiteLLM doesn't already know

LiteLLM ships pricing + adapter for ~100 providers out of the box (see
`litellm --health` after startup for the current catalog). If you need
one it doesn't know — a self-hosted endpoint, a custom API — either
write a LiteLLM custom provider (LiteLLM docs) or add another
`local/<name>` route pointing at your endpoint via the `openai/`
adapter and set `api_base` explicitly.

## 8. The mandatory router (`LLMGateway.Complete`) — how downstream services use it

Any service in the Swarmada monorepo — Go controllers, Python
simulation, external tooling — that needs an LLM MUST call the sidecar's
gRPC surface rather than importing an LLM SDK directly. The rule is
enforced by CI (`.github/workflows/no-direct-llm-sdk.yml` + `make
check-router-only` locally).

The RPC is `sidecar.gateway.v1.LLMGateway/Complete`. Contract lives at
[`sidecar/proto/gateway/v1/gateway.proto`](../proto/gateway/v1/gateway.proto).

**Python callers (in-process)** should import the Python primitive
directly:

```python
from swarmada_sidecar.gateway import Gateway  # or ctx.gateway inside a
                                              # LangGraph node
```

**Cross-language / cross-process callers** (Go, external, another
service): dial `SIDECAR_GRPC_PORT` and call `Complete`. The reply
carries the Langfuse `trace_id` — persist it alongside whatever your
service persists so a human can look the trace up later.
