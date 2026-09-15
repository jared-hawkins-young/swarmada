"""LlamaGuard-backed input safety classifier.

Routes through LiteLLM (configured `llamaguard_model`) for MVP so the image
stays slim. Returns a structured verdict; caller decides whether to block.

Fail-closed: on gateway failure, verdict is `safe=False` with reason
LlamaGuardUnavailable. Never returns SAFE without a real classifier verdict.
"""

from __future__ import annotations

from pydantic import BaseModel

from swarmada_sidecar.config import Config
from swarmada_sidecar.gateway import Gateway, LiteLLMGatewayError


class InputVerdict(BaseModel):
    safe: bool
    reason: str | None = None
    raw: str | None = None


class InputGuardrail:
    def __init__(self, *, cfg: Config, gateway: Gateway) -> None:
        self._cfg = cfg
        self._gateway = gateway

    def check(self, text: str) -> InputVerdict:
        """Classify `text`. Fail-closed on gateway failure."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a safety classifier. Respond with 'SAFE' or "
                    "'UNSAFE: <reason>' only. Do not explain further."
                ),
            },
            {"role": "user", "content": text},
        ]
        try:
            completion = self._gateway.call(
                model=self._cfg.llamaguard_model,
                messages=messages,
                metadata={"purpose": "input_guardrail"},
            )
        except LiteLLMGatewayError as exc:
            return InputVerdict(safe=False, reason=f"LlamaGuardUnavailable: {exc}")

        try:
            content = str(completion["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError):
            return InputVerdict(safe=False, reason="LlamaGuardMalformedResponse")

        if content.upper().startswith("SAFE"):
            return InputVerdict(safe=True, raw=content)
        return InputVerdict(safe=False, reason=content, raw=content)
