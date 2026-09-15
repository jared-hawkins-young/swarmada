# Agent Protocol — how to add an LLM-using node

Contract for any code in this repo (human-written or agent-written) that
needs to call an LLM or add a new step to a `swarmada_sidecar`-backed
pipeline. Non-negotiable per Constitution Principle III (Traced By
Default) and Principle II (Fail-Closed).

## The single rule

**Never call `litellm`, `openai`, `anthropic`, `httpx.post("/chat/...")`,
or any LLM SDK directly.** All LLM traffic goes through
`swarmada_sidecar.gateway.Gateway.call(...)`. That is the only supported
call site in the entire package — verify with:

```bash
grep -rn "litellm\|openai\|anthropic\." sidecar/src/ | grep -v gateway.py
# expected output: nothing
```

`Gateway.call()` handles: LiteLLM proxy routing (config-driven model
swap), the `langfuse_otel` success/failure callback (model + tokens +
cost land in Langfuse automatically), retries with exponential backoff,
per-call timeout, and structured `LiteLLMGatewayError` on retry
exhaustion (fail-closed).

## Adding a new LangGraph node

1. Write the node function in your project's nodes module (e.g.
   `examples/drift-detection/src/drift_detection/graph_nodes.py`):

   ```python
   from swarmada_sidecar.gateway import LiteLLMGatewayError
   from swarmada_sidecar.graph.nodes import NodeContext
   from swarmada_sidecar.graph.state import AgentState

   def my_new_node(state: AgentState, ctx: NodeContext) -> AgentState:
       if state.has_errored():
           return state  # short-circuit: never overwrite an error
       try:
           completion = ctx.gateway.call(
               model=ctx.cfg.active_model,
               messages=[
                   {"role": "system", "content": "..."},
                   {"role": "user", "content": "..."},
               ],
               response_format={"type": "json_object"},
               metadata={"purpose": "my_new_node", "prompt_version": ctx.cfg.langfuse_prompt_version},
           )
       except LiteLLMGatewayError as exc:
           state.errors.append(f"my_new_node_failed: {exc}")
           return state
       # ... parse, validate, update state.payload
       return state
   ```

2. Register it via `swarmada_sidecar.graph.graph.build_graph(...)`:

   ```python
   from swarmada_sidecar.graph.graph import build_graph, conditional_route

   run = build_graph(
       ctx=ctx,
       nodes=[
           ("previous_node", previous_node),
           ("my_new_node", my_new_node),
           ("respond", respond),
       ],
       entry_point="previous_node",
       edges=[("previous_node", "my_new_node")],
       conditional_edges=[
           ("my_new_node", conditional_route("respond"), {"respond": "respond"}),
       ],
       terminal_nodes=["respond"],
       as_type_map={"my_new_node": "span"},  # or "generation", "tool", "guardrail"
   )
   ```

   `build_graph` automatically wraps every node in a Langfuse nested span.
   Every `ctx.gateway.call(...)` inside emits a nested `generation`
   observation with model + tokens + cost. Zero extra wiring needed.

3. If the node opens its own root trace (not nested inside another
   pipeline), wrap the entry point:

   ```python
   with ctx.langfuse.root_pipeline(
       name="my_pipeline",
       input=whatever_the_caller_gave_us,
       metadata={"purpose": "..."},
   ) as trace_id:
       state.langfuse_trace_id = trace_id  # so it lands on the persisted output
       run(state)
   ```

## What lands in Langfuse automatically

- **Trace** — one per top-level pipeline invocation. Root span opened by
  `LangfuseClient.root_pipeline(...)`.
- **Span per node** — automatic via `build_graph`. Node's return value
  becomes the span's `output`. Any exception is recorded on the span,
  level=ERROR, then re-raised (fail-closed).
- **Generation per LLM call** — emitted by LiteLLM's `langfuse_otel`
  callback, registered once in `Gateway.__init__`. Includes
  `provided_model_name`, `provided_usage_details` (input/output/total
  tokens), `provided_cost_details` (dollar cost from LiteLLM's price
  table), and full input/output messages.

## Cost accounting

- LiteLLM computes cost from its built-in `model_prices.json` at every
  successful completion. Real providers (`anthropic/...`,
  `openai/gpt-...`, `gemini/...`) have pricing rows and produce nonzero
  cost. Local routes (`local/...`, any `ollama/...` served through the
  proxy) have no pricing row and legitimately produce `cost=0` — no
  external bill, no compute-time surcharge tracked.
- To attribute local compute cost, add a custom `total_cost` on the
  generation via LiteLLM's `custom_llm_provider_settings` or a
  post-processing job. For MVP we don't; local dev cost is out of scope.

## Model selection

- The active model is `SIDECAR_ACTIVE_MODEL` (env var, a k8s ConfigMap
  in prod). It must match a supported prefix in
  [sidecar/src/swarmada_sidecar/config.py](../src/swarmada_sidecar/config.py)
  `SUPPORTED_MODEL_PATTERNS`.
- The LlamaGuard input-safety model is `SIDECAR_LLAMAGUARD_MODEL`.
- **Model catalog** (routes, backends, virtual keys, cost caps) is
  managed in the LiteLLM proxy UI at `${SIDECAR_LITELLM_HOST}/ui`, not
  in this repo. That's the intended separation: proxy owns backend
  choice + auth; sidecar owns which routed name to invoke.

## Prompt registry

- All prompts live in Langfuse's prompt registry, keyed by name +
  version label. The active prompt is
  `SIDECAR_LANGFUSE_PROMPT_NAME` @ `SIDECAR_LANGFUSE_PROMPT_VERSION`
  (a label like `v1` or `production`).
- Seed new prompts via [sidecar/scripts/seed_prompt.py](../scripts/seed_prompt.py).
  Never embed prompt text in `src/`.

## Verification checklist for new LLM-using code

Before opening a PR:

- [ ] No direct SDK imports outside `sidecar/src/swarmada_sidecar/gateway.py`
      (`grep -rn "litellm\|openai\|anthropic\." sidecar/src/` returns
      only `gateway.py`).
- [ ] Real e2e run: bring up ollama + LiteLLM proxy + Langfuse; fire the
      code path; open Langfuse UI at `http://localhost:3000` and
      confirm:
      - one trace per invocation with your node's name
      - a nested `generation` observation with the correct model name
        and nonzero token counts
- [ ] Fail-closed path: kill LiteLLM proxy mid-run, re-fire, verify the
      pipeline records an error on the span and does NOT persist a
      partial result.
