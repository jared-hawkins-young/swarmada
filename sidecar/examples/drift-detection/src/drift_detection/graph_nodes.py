"""LangGraph nodes for the drift-diagnosis workflow.

Every node is wrapped by the Langfuse trace-span middleware (Constitution
Principle III) via `swarmada_sidecar.graph.graph.build_graph`. Every node
is fail-closed (Principle II): failures propagate through the state's
`errors` list and the compiled graph short-circuits to `respond`.

Intermediate state (window_snapshot, reasons, guardrail verdicts, LLM
response) rides in `state.payload` since foundation `AgentState`
intentionally has no drift-specific fields.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import structlog
from pydantic import BaseModel, Field, field_validator

from drift_detection.drift_metrics import (
    diagnosis_pipeline_latency_seconds,
    diagnosis_row_count,
)
from drift_detection.store import DriftStore
from swarmada_sidecar.gateway import LiteLLMGatewayError
from swarmada_sidecar.graph.nodes import NodeContext
from swarmada_sidecar.graph.state import AgentState
from swarmada_sidecar.metrics import guardrail_blocks_total

log = structlog.get_logger(__name__)


# --- Output schema (moved out of the foundation OutputGuardrail) ---


class RecommendedAction(str, Enum):
    RETRAIN = "RETRAIN"
    RECALIBRATE_SENSOR = "RECALIBRATE_SENSOR"
    INSPECT_HARDWARE = "INSPECT_HARDWARE"
    INVESTIGATE = "INVESTIGATE"
    NO_ACTION = "NO_ACTION"


class DriftDiagnosisPayload(BaseModel):
    """Schema the LLM's structured JSON response MUST satisfy.

    Fed into `swarmada_sidecar.guardrails.output_guardrails_ai.OutputGuardrail`
    at construction time (`OutputGuardrail(schema=DriftDiagnosisPayload)`).
    """

    root_cause_hypotheses: list[str] = Field(min_length=1, max_length=5)
    recommended_action: RecommendedAction
    confidence: float = Field(ge=0.0, le=1.0)
    human_readable_summary: str = Field(min_length=1, max_length=1000)

    @field_validator("root_cause_hypotheses")
    @classmethod
    def _hypotheses_bounded(cls, v: list[str]) -> list[str]:
        for h in v:
            if not h or len(h) > 500:
                raise ValueError(
                    "each hypothesis must be non-empty and <= 500 chars"
                )
        return v


# --- DriftNodeContext extends NodeContext with a domain store ---


class DriftNodeContext(NodeContext):
    """Adds a DriftStore reference so persist() can write diagnoses."""

    def __init__(self, *, store: DriftStore, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.store = store


# --- nodes ---
#
# The graph builder (`swarmada_sidecar.graph.graph.build_graph`) binds each
# node with a `NodeContext`; drift nodes rely on the DriftNodeContext
# subclass for `.store`. Type-narrow at the top of each node that needs it.


def fetch_window(state: AgentState, ctx: NodeContext) -> AgentState:
    """Copy window snapshot and reasons from the drift event into state.payload."""
    drift_event = state.payload.get("drift_event", {})
    state.payload["window_snapshot"] = drift_event.get("window_snapshot", {})
    state.payload["reasons"] = list(drift_event.get("reasons", []))
    return state


def classify_drift(state: AgentState, ctx: NodeContext) -> AgentState:
    """Passthrough for now; future work can compute additional context
    (severity, affinity to prior events) before the LLM call."""
    return state


def guardrail_input(state: AgentState, ctx: NodeContext) -> AgentState:
    """Run LlamaGuard over operator-provided input carried in the drift
    event. Skipped by default because the drift event is system-generated
    telemetry (robot IDs, model versions, class predictions), not
    operator text — LlamaGuard classifies that internal jargon as
    adversarial in isolation.

    If a future capability adds an `operator_note` field to the drift
    event (e.g. a natural-language annotation entered in the ops UI),
    THAT field flows through LlamaGuard here. See RFC-0002 §5.
    """
    drift_event = state.payload.get("drift_event", {})
    operator_text = drift_event.get("operator_note")
    if not operator_text:
        state.payload["input_guardrail_verdict"] = {
            "safe": True,
            "reason": "no_operator_input",
        }
        return state
    verdict = ctx.input_guardrail.check(str(operator_text))
    state.payload["input_guardrail_verdict"] = verdict.model_dump()
    if not verdict.safe:
        guardrail_blocks_total.labels(
            check_type="INPUT_LLAMAGUARD",
            reason=verdict.reason or "UNKNOWN",
        ).inc()
        state.errors.append(f"input_guardrail_blocked: {verdict.reason}")
    return state


def llm_diagnose(state: AgentState, ctx: NodeContext) -> AgentState:
    """Call the LLM through LiteLLM. Fail-closed on gateway or parse error."""
    if state.has_errored():
        return state
    prompt = ctx.langfuse.get_prompt()
    user_payload = {
        "reasons": state.payload.get("reasons", []),
        "window_snapshot": state.payload.get("window_snapshot", {}),
    }
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(user_payload, sort_keys=True)},
    ]
    try:
        completion = ctx.gateway.call(
            model=ctx.cfg.active_model,
            messages=messages,
            response_format={"type": "json_object"},
            metadata={
                "prompt_version": ctx.cfg.langfuse_prompt_version,
                "drift_reasons": state.payload.get("reasons", []),
            },
        )
    except LiteLLMGatewayError as exc:
        state.errors.append(f"llm_gateway_failure: {exc}")
        return state

    # Extract the assistant's structured JSON content from the completion.
    try:
        content = completion["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        state.errors.append(f"llm_response_unparseable: {exc}")
        return state

    state.payload["llm_response"] = parsed
    return state


def guardrail_output(state: AgentState, ctx: NodeContext) -> AgentState:
    """Validate the LLM's structured JSON output. Fail-closed on any schema
    violation, missing field, or type mismatch."""
    if state.has_errored():
        return state
    llm_response = state.payload.get("llm_response")
    assert llm_response is not None  # invariant from llm_diagnose
    verdict = ctx.output_guardrail.validate(llm_response)
    state.payload["output_guardrail_verdict"] = verdict.model_dump()
    if not verdict.valid:
        guardrail_blocks_total.labels(
            check_type="OUTPUT_GUARDRAILS_AI",
            reason=verdict.reason or "UNKNOWN",
        ).inc()
        state.errors.append(f"output_guardrail_blocked: {verdict.reason}")
    return state


def persist(state: AgentState, ctx: NodeContext) -> AgentState:
    """Write the diagnosis to the SQLite store keyed by correlation IDs."""
    if state.has_errored():
        return state
    assert isinstance(ctx, DriftNodeContext), (
        "persist() requires a DriftNodeContext; wire the drift-detection graph "
        "with DriftNodeContext, not the bare foundation NodeContext."
    )
    llm_response = state.payload.get("llm_response")
    assert llm_response is not None

    drift_event = state.payload.get("drift_event", {})
    window_key = drift_event.get("window_key", ["", "", "", ""])
    robot_id = window_key[0] if len(window_key) > 0 else ""
    model_id = window_key[1] if len(window_key) > 1 else ""
    model_version = window_key[2] if len(window_key) > 2 else ""
    task_type = window_key[3] if len(window_key) > 3 else ""
    fleet_action_id = drift_event.get("triggering_fleet_action_id", "")

    diagnosis = {
        "robot_id": robot_id,
        "fleet_action_id": fleet_action_id,
        "model_id": model_id,
        "model_version": model_version,
        "task_type": task_type,
        "root_cause_hypotheses": llm_response.get("root_cause_hypotheses", []),
        "recommended_action": llm_response.get("recommended_action", "INVESTIGATE"),
        "confidence": float(llm_response.get("confidence", 0.0)),
        "human_readable_summary": llm_response.get("human_readable_summary", ""),
        "model_used": ctx.cfg.active_model,
        "prompt_version": ctx.cfg.langfuse_prompt_version,
        "created_at": datetime.now(UTC).isoformat(),
        "langfuse_trace_id": state.langfuse_trace_id or "",
    }

    ctx.store.insert_diagnosis(
        robot_id=robot_id,
        fleet_action_id=fleet_action_id,
        payload=diagnosis,
        prompt_version=ctx.cfg.langfuse_prompt_version,
        model_used=ctx.cfg.active_model,
        langfuse_trace_id=state.langfuse_trace_id or "",
    )
    diagnosis_row_count.set(ctx.store.diagnosis_count())

    state.payload["diagnosis"] = diagnosis
    return state


def respond(state: AgentState, ctx: NodeContext) -> AgentState:
    """Terminal node. Emits pipeline latency and returns final state."""
    drift_event = state.payload.get("drift_event", {})
    triggered = drift_event.get("triggered_at_epoch", 0.0)
    if triggered:
        import time as _time

        diagnosis_pipeline_latency_seconds.observe(_time.time() - triggered)
    return state


DRIFT_NODES: list[tuple[str, Any]] = [
    ("fetch_window", fetch_window),
    ("classify_drift", classify_drift),
    ("guardrail_input", guardrail_input),
    ("llm_diagnose", llm_diagnose),
    ("guardrail_output", guardrail_output),
    ("persist", persist),
    ("respond", respond),
]


DRIFT_AS_TYPE_MAP: dict[str, str] = {
    "fetch_window": "span",
    "classify_drift": "span",
    "guardrail_input": "guardrail",
    "llm_diagnose": "span",  # LiteLLM emits its own generation observation
    "guardrail_output": "guardrail",
    "persist": "span",
    "respond": "span",
}
