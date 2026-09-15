"""gRPC servicer for the sidecar's mandatory LLM Gateway RPC.

Every Complete call flows through the LiteLLM proxy (config-driven model
routing) and emits a Langfuse root_pipeline trace + generation observation
(model, tokens, cost). Callers get back the response plus the Langfuse
trace_id for downstream correlation.

Language-agnostic: Go controllers, Python simulation, external tooling
all speak gRPC. The repo-root CI check (`make check-router-only`) forbids
direct LLM SDK imports anywhere else in the monorepo.
"""

from __future__ import annotations

from typing import Any

import grpc
import structlog

from swarmada_sidecar.config import Config
from swarmada_sidecar.gateway import Gateway, LiteLLMGatewayError
from swarmada_sidecar.guardrails.input_llamaguard import InputGuardrail
from swarmada_sidecar.langfuse_client import LangfuseClient
from swarmada_sidecar.metrics import guardrail_blocks_total

log = structlog.get_logger(__name__)


class LLMGatewayServicer:
    """gRPC handler for `sidecar.gateway.v1.LLMGateway`.

    Imports the generated pb2 modules lazily so importing this module does
    not require `make proto-gen` to have been run yet (matches the pattern
    used by the drift example's servicer).
    """

    def __init__(
        self,
        *,
        cfg: Config,
        gateway: Gateway,
        langfuse: LangfuseClient,
        input_guardrail: InputGuardrail | None = None,
    ) -> None:
        self._cfg = cfg
        self._gateway = gateway
        self._langfuse = langfuse
        self._input_guardrail = input_guardrail
        from swarmada_sidecar.proto_gen.gateway.v1 import (
            gateway_pb2,
            gateway_pb2_grpc,
        )

        self._pb2 = gateway_pb2
        self._pb2_grpc = gateway_pb2_grpc

    def Complete(  # noqa: N802 — gRPC convention
        self,
        request: Any,
        context: grpc.ServicerContext,
    ) -> Any:
        """Route a chat completion through the sidecar's LiteLLM + Langfuse
        primitive. Fail-closed on guardrail block, retry exhaustion, or
        malformed model response — all surface as gRPC status codes.
        """
        model = request.model or self._cfg.active_model
        trace_name = request.trace_name or "llm_gateway_complete"
        response_format: dict[str, Any] | None = None
        if request.response_format.type == self._pb2.ResponseFormat.JSON_OBJECT:
            response_format = {"type": "json_object"}
        metadata = dict(request.metadata) if request.metadata else {}
        metadata.setdefault("sidecar.entry_rpc", "LLMGateway.Complete")

        messages = [
            {"role": m.role, "content": m.content} for m in request.messages
        ]

        with self._langfuse.root_pipeline(
            name=trace_name,
            input={"model": model, "messages": messages},
            metadata=metadata,
        ) as trace_id:
            # Optional: run LlamaGuard on operator_input when both are
            # present. Callers that already ran their own safety check
            # leave operator_input empty and skip this.
            if request.operator_input and self._input_guardrail is not None:
                verdict = self._input_guardrail.check(request.operator_input)
                if not verdict.safe:
                    guardrail_blocks_total.labels(
                        check_type="INPUT_LLAMAGUARD",
                        reason=verdict.reason or "UNKNOWN",
                    ).inc()
                    context.set_code(grpc.StatusCode.PERMISSION_DENIED)
                    context.set_details(
                        f"input_guardrail_blocked: {verdict.reason}"
                    )
                    return self._pb2.CompleteResponse(
                        content="",
                        model=model,
                        trace_id=trace_id,
                        usage=self._pb2.Usage(),
                    )

            try:
                completion = self._gateway.call(
                    model=model,
                    messages=messages,
                    response_format=response_format,
                    metadata=metadata,
                )
            except LiteLLMGatewayError as exc:
                log.error("llm_gateway.complete_failed", error=str(exc))
                context.set_code(grpc.StatusCode.UNAVAILABLE)
                context.set_details(f"litellm_gateway_failure: {exc}")
                return self._pb2.CompleteResponse(
                    content="",
                    model=model,
                    trace_id=trace_id,
                    usage=self._pb2.Usage(),
                )

            try:
                choice = completion["choices"][0]
                content = str(choice["message"]["content"])
            except (KeyError, IndexError, TypeError) as exc:
                log.error("llm_gateway.malformed_response", error=str(exc))
                context.set_code(grpc.StatusCode.INTERNAL)
                context.set_details(f"malformed_llm_response: {exc}")
                return self._pb2.CompleteResponse(
                    content="",
                    model=model,
                    trace_id=trace_id,
                    usage=self._pb2.Usage(),
                )

            usage_raw = completion.get("usage") or {}
            served_model = str(completion.get("model") or model)

            return self._pb2.CompleteResponse(
                content=content,
                model=served_model,
                trace_id=trace_id,
                usage=self._pb2.Usage(
                    input_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
                    output_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
                    total_tokens=int(usage_raw.get("total_tokens", 0) or 0),
                ),
            )

    def register(self, server: grpc.Server) -> None:
        """Register this servicer on a gRPC server."""
        self._pb2_grpc.add_LLMGatewayServicer_to_server(self, server)
