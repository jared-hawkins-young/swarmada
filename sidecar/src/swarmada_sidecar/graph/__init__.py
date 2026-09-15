"""LangGraph orchestration primitives.

The foundation ships a domain-neutral `AgentState`, a `NodeContext` for
dependency injection, and a `build_graph` helper that wires an arbitrary
list of nodes + edges into a compiled, Langfuse-traced `StateGraph`.
Callers wire their own graph shape.
"""
