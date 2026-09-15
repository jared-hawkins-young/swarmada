"""Foundation configuration for the sidecar.

Loaded from environment (Kubernetes ConfigMap projection) and secrets
(Kubernetes Secret projection).

Per Constitution Principle II (Fail-Closed by Default): required fields
without a default MUST fail startup if unset. Pydantic v2 raises
ValidationError; the server main() propagates that error and refuses to
enter Ready state.

Domain-specific configuration (e.g. drift-detection thresholds and
retention windows) lives alongside its consumer — see
`examples/drift-detection/src/drift_detection/drift_config.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Supported LiteLLM model identifiers.
# Extending this list is a config change; adding a new PROVIDER family may
# require gateway rewiring, which is a code change.
SUPPORTED_MODEL_PATTERNS: tuple[str, ...] = (
    # Anthropic
    "anthropic/",
    "claude-",
    # OpenAI
    "openai/",
    "gpt-",
    # Google
    "gemini/",
    "google/",
    # Meta / open weights via Ollama, Together, or similar
    "ollama/",
    "together_ai/",
    "meta-llama/",
    "llama-",
    # HuggingFace inference endpoint proxy through LiteLLM
    "huggingface/",
    # Local dev / self-hosted routes named honestly by the LiteLLM proxy
    # (e.g. `local/llama3.2:1b`, `local/llamaguard-mock`). Prod overrides
    # active_model to the real provider-prefixed name.
    "local/",
)


class Config(BaseSettings):
    """Sidecar foundation runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="SIDECAR_",
        env_file=None,  # ConfigMap projection is the source of truth in-cluster
        case_sensitive=False,
        # Fail-fast on unknown env vars — a typo like `SIDECAR_ACTIVEMODEL`
        # must not silently no-op. Downstream configs (e.g. DriftConfig)
        # MUST use a distinct env_prefix (SIDECAR_DRIFT_) rather than
        # relying on this Config to ignore their vars.
        extra="forbid",
    )

    # --- Model selection ---
    active_model: str = Field(
        default="anthropic/claude-3.5-sonnet",
        description="LiteLLM model identifier. Swap by config, never code.",
    )

    # --- Langfuse ---
    langfuse_host: str = Field(..., description="Langfuse endpoint URL (required)")
    langfuse_public_key: SecretStr = Field(...)
    langfuse_secret_key: SecretStr = Field(...)
    langfuse_prompt_name: str = Field(default="drift-diagnosis")
    langfuse_prompt_version: str = Field(
        ...,
        description="Pinned Langfuse prompt version tag (required)",
    )

    # --- LiteLLM ---
    litellm_host: str = Field(..., description="LiteLLM proxy endpoint URL (required)")
    litellm_master_key: SecretStr | None = Field(
        default=None,
        description="Optional; only required if LiteLLM proxy has auth enabled.",
    )
    litellm_timeout_seconds: float = Field(default=3.0, gt=0.0)
    litellm_max_retries: int = Field(default=3, ge=0, le=10)

    # --- LlamaGuard ---
    llamaguard_model: str = Field(default="meta-llama/LlamaGuard-3-8B")

    # --- Storage ---
    sqlite_path: Path = Field(default=Path("/var/lib/sidecar/state.db"))

    # --- Ports ---
    grpc_port: int = Field(default=50051, ge=1024, le=65535)
    metrics_port: int = Field(default=9090, ge=1024, le=65535)

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")

    def validate_active_model(self) -> None:
        """Ensure active_model matches a supported provider family.

        Called at startup. Raises ValueError (fail-closed) on mismatch.
        """
        model = self.active_model.lower()
        if not any(model.startswith(p.lower()) for p in SUPPORTED_MODEL_PATTERNS):
            raise ValueError(
                f"active_model={self.active_model!r} does not match any supported "
                f"provider pattern. Supported prefixes: {SUPPORTED_MODEL_PATTERNS}"
            )


def load_config() -> Config:
    """Load and validate configuration. Raises on failure (fail-closed)."""
    cfg = Config()  # type: ignore[call-arg]  # pydantic-settings picks up env
    cfg.validate_active_model()
    return cfg
