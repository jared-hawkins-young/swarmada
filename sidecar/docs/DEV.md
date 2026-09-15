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
