"""Foundation LangGraph state schema.

`AgentState` is deliberately minimal: an opaque `payload` dict for input,
an `errors` list for fail-closed short-circuit routing, and a
`langfuse_trace_id` for provenance.

Downstream agents extend `AgentState` (subclass) or stash intermediate
state in `payload` to carry richer context between nodes. Any node error
propagates through `errors` and short-circuits the graph — no node
silently defaults to a happy-path value (Constitution Principle II).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentState(BaseModel):
    """Carrier of state across LangGraph nodes."""

    model_config = ConfigDict(extra="allow", frozen=False)

    # --- Input / opaque intermediate state ---
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="Domain-specific payload. Structure is caller-owned.",
    )

    # --- Provenance ---
    langfuse_trace_id: str | None = None

    # --- Error path ---
    errors: list[str] = Field(default_factory=list)

    def has_errored(self) -> bool:
        return bool(self.errors)
