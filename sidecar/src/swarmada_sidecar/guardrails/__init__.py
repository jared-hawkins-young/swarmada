"""Input + output guardrails.

Two layers per Constitution Principle II:
- InputGuardrail: LlamaGuard-backed safety classifier on operator-provided
  text (routed through LiteLLM).
- OutputGuardrail: Guardrails AI schema validator on the LLM's structured
  JSON output — parameterized on any Pydantic BaseModel schema the caller
  supplies.

Both fail-closed. Any block returns a failed verdict that the LangGraph
node propagates through state.errors, short-circuiting the graph.
"""
