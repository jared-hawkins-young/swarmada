"""LiteLLM gateway wrapper.

Model-swappable via config. Retries with exponential backoff. Per-call
timeout enforced. Every call attaches the Langfuse callback for tracing.

Fail-closed posture: retry exhaustion raises LiteLLMGatewayError; caller
decides whether to abort the pipeline or degrade. No silent fallback.
"""

from __future__ import annotations

import time
from typing import Any

import litellm  # type: ignore[import-untyped]
import structlog

from swarmada_sidecar.config import Config
from swarmada_sidecar.metrics import llm_call_latency_seconds, llm_gateway_failures_total

log = structlog.get_logger(__name__)


class LiteLLMGatewayError(RuntimeError):
    """Raised when the LiteLLM gateway fails (retries exhausted, malformed,
    provider outage). Callers must handle explicitly — no silent default."""


class Gateway:
    """LiteLLM-backed LLM gateway."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        # Route every `litellm.completion()` through the self-hosted LiteLLM
        # proxy as an OpenAI-compatible endpoint. The proxy handles routing
        # to the actual model backend (ollama, anthropic, etc.).
        self._api_base = cfg.litellm_host
        self._api_key = (
            cfg.litellm_master_key.get_secret_value()
            if cfg.litellm_master_key is not None
            else "sk-anything"  # proxy in unauthenticated mode ignores this
        )
        # Constitution Principle III: every LLM call must be traced.
        # `langfuse_otel` is LiteLLM's OTel-based callback compatible with
        # Langfuse SDK 4.x. It emits a generation observation (model name,
        # input/output messages, token usage, cost) nested under whatever
        # Langfuse span is currently active (the root_pipeline span opened
        # by the caller). Reads LANGFUSE_HOST / _PUBLIC_KEY / _SECRET_KEY
        # from the environment, which LangfuseClient.__init__ exports. The
        # older `langfuse` callback expects langfuse 2.x and is not used
        # here.
        callbacks = list(getattr(litellm, "success_callback", []) or [])
        if "langfuse_otel" not in callbacks:
            callbacks.append("langfuse_otel")
        litellm.success_callback = callbacks  # type: ignore[attr-defined]
        failure_callbacks = list(getattr(litellm, "failure_callback", []) or [])
        if "langfuse_otel" not in failure_callbacks:
            failure_callbacks.append("langfuse_otel")
        litellm.failure_callback = failure_callbacks  # type: ignore[attr-defined]

    def health_check(self) -> None:
        """Best-effort connectivity check. Raises on failure (fail-closed).

        Uses `/health/liveness` — a fast, no-side-effect endpoint that
        confirms the proxy process is up. `/health` on LiteLLM tries to
        ping every configured backend and can take tens of seconds, which
        is a startup-latency trap.
        """
        import urllib.error
        import urllib.request

        base = self._cfg.litellm_host.rstrip("/")
        # LiteLLM exposes both /health/liveness (fast) and /health (slow,
        # calls every backend). Prefer the fast one; fall back to the
        # legacy /health path if the proxy version doesn't expose it.
        last_exc: Exception = RuntimeError("no attempt made")
        for path in ("/health/liveness", "/health"):
            try:
                req = urllib.request.Request(base + path, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status < 500:
                        return
            except (urllib.error.URLError, TimeoutError) as exc:
                last_exc = exc
                continue
        raise LiteLLMGatewayError(
            f"LiteLLM health check failed for host={self._cfg.litellm_host!r}: {last_exc}"
        ) from last_exc

    def call(
        self,
        *,
        model: str | None,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Invoke an LLM through LiteLLM with retry + timeout.

        `model` defaults to the configured active_model. `response_format`
        enables structured JSON output when the underlying model supports
        it (OpenAI-compatible schema). `metadata` is passed to Langfuse via
        LiteLLM's callback so traces carry correlation IDs.

        Raises LiteLLMGatewayError if all retries fail.
        """
        used_model = model or self._cfg.active_model
        delays = [0.5 * (2**i) for i in range(self._cfg.litellm_max_retries)]
        last_exc: Exception | None = None

        for attempt, delay in enumerate([0.0, *delays]):
            if delay:
                time.sleep(delay)
            started = time.perf_counter()
            try:
                completion = litellm.completion(
                    model=used_model,
                    messages=messages,
                    timeout=self._cfg.litellm_timeout_seconds,
                    response_format=response_format,
                    metadata=metadata or {},
                    api_base=self._api_base,
                    api_key=self._api_key,
                    custom_llm_provider="openai",
                )
                elapsed = time.perf_counter() - started
                llm_call_latency_seconds.labels(model=used_model).observe(elapsed)
                return completion.model_dump() if hasattr(completion, "model_dump") else dict(completion)  # type: ignore[no-any-return]
            except Exception as exc:  # noqa: BLE001 — LiteLLM raises varied types
                last_exc = exc
                log.warning(
                    "litellm.call.attempt_failed",
                    attempt=attempt,
                    model=used_model,
                    error=str(exc),
                )

        # Retry budget exhausted. Fail-closed.
        llm_gateway_failures_total.labels(
            model=used_model,
            reason=type(last_exc).__name__ if last_exc else "unknown",
        ).inc()
        raise LiteLLMGatewayError(
            f"LiteLLM call to model={used_model!r} failed after "
            f"{self._cfg.litellm_max_retries} retries"
        ) from last_exc
