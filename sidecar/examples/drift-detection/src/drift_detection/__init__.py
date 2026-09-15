"""drift-detection — reference implementation of a swarmada_sidecar agent.

Composes the swarmada_sidecar foundation primitives (LiteLLM gateway,
Langfuse tracing, LangGraph orchestration, LlamaGuard input guardrail,
Guardrails AI output guardrail) into a rolling-window drift diagnosis
pipeline for a fleet of autonomous robots.

Entry point: `python -m drift_detection` (see `__main__.py`).
"""

__version__ = "0.1.0"
