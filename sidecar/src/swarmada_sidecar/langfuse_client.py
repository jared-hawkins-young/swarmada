"""Langfuse client wrapper.

Provides:
- health-check helper (used at startup; fail-closed if unreachable)
- prompt-registry fetch by name+version
- trace-span middleware for wrapping any callable

Per Constitution Principle III (Traced By Default): every LLM call and
LangGraph node MUST be wrapped by this middleware. Un-traced inference in
production paths is a bug.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any, ParamSpec, TypeVar

import structlog
from langfuse import Langfuse  # type: ignore[import-not-found]

from swarmada_sidecar.config import Config

log = structlog.get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


class LangfuseUnavailableError(RuntimeError):
    """Raised when Langfuse cannot be reached at startup. Fail-closed."""


class LangfuseClient:
    """Thin adapter around the Langfuse SDK (3.x/4.x, OpenTelemetry-based)."""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        # Export the Langfuse credentials so LiteLLM's built-in Langfuse
        # callback (registered in gateway.py) can find them. LiteLLM reads
        # LANGFUSE_HOST / _PUBLIC_KEY / _SECRET_KEY from the environment.
        os.environ.setdefault("LANGFUSE_HOST", cfg.langfuse_host)
        os.environ.setdefault(
            "LANGFUSE_PUBLIC_KEY", cfg.langfuse_public_key.get_secret_value()
        )
        os.environ.setdefault(
            "LANGFUSE_SECRET_KEY", cfg.langfuse_secret_key.get_secret_value()
        )
        self._lf = Langfuse(
            host=cfg.langfuse_host,
            public_key=cfg.langfuse_public_key.get_secret_value(),
            secret_key=cfg.langfuse_secret_key.get_secret_value(),
        )

    # --- health ---

    def health_check(self) -> None:
        """Verify Langfuse reachable. Raises on failure (fail-closed)."""
        try:
            # Auth check pings the API; raises on 401/network failure.
            self._lf.auth_check()
        except Exception as exc:  # noqa: BLE001 — SDK raises varied types
            raise LangfuseUnavailableError(
                f"Langfuse health check failed for host={self._cfg.langfuse_host!r}: {exc}"
            ) from exc

    # --- prompt registry ---

    def get_prompt(self) -> str:
        """Fetch the pinned prompt from Langfuse. Raises on missing.

        `langfuse_prompt_version` is passed as an integer to `version` when
        it parses as one, otherwise as a `label` (Langfuse pins ranges via
        labels like "v1" or "production", and revisions via numeric `version`).
        """
        raw = self._cfg.langfuse_prompt_version
        try:
            numeric_version: int | None = int(raw)
            label: str | None = None
        except (TypeError, ValueError):
            numeric_version = None
            label = str(raw)

        try:
            prompt = self._lf.get_prompt(
                name=self._cfg.langfuse_prompt_name,
                version=numeric_version,
                label=label,
            )
        except Exception as exc:  # noqa: BLE001
            raise LangfuseUnavailableError(
                f"Failed to fetch prompt "
                f"{self._cfg.langfuse_prompt_name}@{raw} "
                f"from Langfuse: {exc}"
            ) from exc
        return prompt.prompt  # SDK returns a Prompt object

    # --- trace middleware (Langfuse 3.x/4.x, OTel-based) ---

    @contextmanager
    def root_pipeline(
        self,
        *,
        name: str,
        input: Any | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """Open a root chain span for one pipeline invocation.

        Yields the trace_id string so the caller can persist it alongside
        any produced output for correlation.
        """
        with self._lf.start_as_current_observation(
            name=name,
            as_type="chain",
            input=input,
            metadata=metadata or {},
        ):
            trace_id = self._lf.get_current_trace_id() or ""
            yield trace_id

    def trace_span(
        self,
        name: str,
        *,
        as_type: str = "span",
        metadata: dict[str, Any] | None = None,
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Decorator that wraps a callable in a nested Langfuse span.

        Must be called inside a `root_pipeline(...)` context — otherwise
        the emitted span becomes its own root trace. Fail-closed: any
        exception in the wrapped callable is recorded on the span and
        re-raised (Constitution Principle II).
        """

        def decorator(fn: Callable[P, R]) -> Callable[P, R]:
            @wraps(fn)
            def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
                with self._lf.start_as_current_observation(
                    name=name,
                    as_type=as_type,  # type: ignore[arg-type]
                    metadata=metadata or {},
                ) as span:
                    try:
                        result = fn(*args, **kwargs)
                        if result is not None:
                            try:
                                span.update(output=result)
                            except Exception:  # noqa: BLE001
                                pass  # never let telemetry mask real work
                        return result
                    except Exception as exc:  # noqa: BLE001
                        span.update(
                            level="ERROR", status_message=str(exc)
                        )
                        raise

            return wrapped

        return decorator

    def flush(self) -> None:
        """Force pending traces to send. Called on shutdown."""
        self._lf.flush()
