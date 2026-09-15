"""Foundation LangGraph builder.

`build_graph(...)` compiles a `StateGraph` from a caller-supplied list of
nodes + edges. Every node is wrapped in a nested Langfuse span so it
shows up in the trace tree (Constitution Principle III).

Fail-closed short-circuit routing is provided via `conditional_route`:
any node that appends to `state.errors` diverts subsequent edges to a
terminal `respond`-style node. Callers supply the actual node function
map and edge topology.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from langgraph.graph import END, StateGraph  # type: ignore[import-not-found]

from swarmada_sidecar.graph.nodes import NodeContext
from swarmada_sidecar.graph.state import AgentState

# Callers may extend or override this per node to control the observation
# type surfaced in the Langfuse UI ("span", "guardrail", "generation",
# "tool", "chain"). Defaults to "span" when a node name is not present.
_NODE_AS_TYPE_DEFAULT: dict[str, str] = {
    "guardrail_input": "guardrail",
    "guardrail_output": "guardrail",
}


NodeFn = Callable[[AgentState, NodeContext], AgentState]


def _bind(
    fn: NodeFn,
    ctx: NodeContext,
    node_name: str,
    as_type_map: dict[str, str],
) -> Callable[[AgentState], AgentState]:
    """Wrap `fn` with a nested Langfuse span so each node shows up in the
    trace tree. The wrapper is a no-op if called outside a root pipeline
    context — every real invocation of the graph should open that root
    via `LangfuseClient.root_pipeline(...)` before running the compiled
    graph."""
    as_type = as_type_map.get(node_name, "span")
    traced = ctx.langfuse.trace_span(node_name, as_type=as_type)(fn)

    def bound(state: AgentState) -> AgentState:
        return traced(state, ctx)

    return bound


def build_graph(
    *,
    ctx: NodeContext,
    nodes: Iterable[tuple[str, NodeFn]],
    entry_point: str,
    edges: Iterable[tuple[str, str]] = (),
    conditional_edges: Iterable[
        tuple[str, Callable[[AgentState], str], dict[str, str]]
    ] = (),
    terminal_nodes: Iterable[str] = ("respond",),
    as_type_map: dict[str, str] | None = None,
    state_schema: type[AgentState] = AgentState,
) -> Callable[[AgentState], AgentState]:
    """Compile a StateGraph from a caller-supplied topology.

    Args:
        ctx: NodeContext (or subclass) passed into every bound node.
        nodes: iterable of (node_name, node_fn) pairs.
        entry_point: name of the first node.
        edges: unconditional edges as (src, dst) pairs.
        conditional_edges: (src, router_fn, branch_map) triples where
            `router_fn(state) -> str` picks the branch key and
            `branch_map[branch_key] -> dst_node` resolves it.
        terminal_nodes: node names that connect to END.
        as_type_map: overrides / extends the default Langfuse
            observation type per node name. Merged over
            `_NODE_AS_TYPE_DEFAULT`.
        state_schema: subclass of AgentState if the caller wants a
            richer state type (LangGraph uses it for schema validation).

    Returns a `run(state) -> AgentState` callable. LangGraph returns the
    final state as a dict; `run` hydrates it back into `state_schema`.
    """
    merged_types: dict[str, str] = dict(_NODE_AS_TYPE_DEFAULT)
    if as_type_map:
        merged_types.update(as_type_map)

    g: StateGraph[AgentState] = StateGraph(state_schema)

    node_names: list[str] = []
    for name, fn in nodes:
        g.add_node(name, _bind(fn, ctx, name, merged_types))
        node_names.append(name)

    if entry_point not in node_names:
        raise ValueError(
            f"entry_point={entry_point!r} not in registered nodes: {node_names!r}"
        )
    g.set_entry_point(entry_point)

    for src, dst in edges:
        g.add_edge(src, dst)

    for src, router_fn, branch_map in conditional_edges:
        g.add_conditional_edges(src, router_fn, branch_map)

    for terminal in terminal_nodes:
        if terminal in node_names:
            g.add_edge(terminal, END)

    compiled = g.compile()

    def run(state: AgentState) -> AgentState:
        final: Any = compiled.invoke(state)
        # LangGraph returns the state as a dict; hydrate back.
        return state_schema.model_validate(final)

    return run


def conditional_route(
    ok_node: str, *, err_node: str = "respond"
) -> Callable[[AgentState], str]:
    """Standard fail-closed router: divert to `err_node` on state.has_errored(),
    else proceed to `ok_node`. Use in `conditional_edges` for typical
    fail-closed pipelines."""

    def route(state: AgentState) -> str:
        return err_node if state.has_errored() else ok_node

    return route
