"""Foundation LangGraph node primitives.

Ships only `NodeContext` — the dependency-injection carrier that every
domain node receives as its second argument. Domain node functions live
alongside their consumer (see
`examples/drift-detection/src/drift_detection/graph_nodes.py`).
"""

from __future__ import annotations

from swarmada_sidecar.config import Config
from swarmada_sidecar.gateway import Gateway
from swarmada_sidecar.guardrails.input_llamaguard import InputGuardrail
from swarmada_sidecar.guardrails.output_guardrails_ai import OutputGuardrail
from swarmada_sidecar.langfuse_client import LangfuseClient


class NodeContext:
    """Injected into every node so it can reach dependencies. Not part of
    the graph state itself; carried out-of-band.

    Downstream projects may subclass `NodeContext` to attach additional
    domain-specific dependencies (e.g. a domain store, a domain-specific
    aggregator) and pass the subclass through `build_graph`.
    """

    def __init__(
        self,
        *,
        cfg: Config,
        gateway: Gateway,
        langfuse: LangfuseClient,
        input_guardrail: InputGuardrail,
        output_guardrail: OutputGuardrail,
    ) -> None:
        self.cfg = cfg
        self.gateway = gateway
        self.langfuse = langfuse
        self.input_guardrail = input_guardrail
        self.output_guardrail = output_guardrail
