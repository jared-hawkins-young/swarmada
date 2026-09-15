"""Guardrails AI output validator.

Validates the LLM's structured JSON response against a caller-supplied
Pydantic BaseModel schema. Fail-closed: any missing required field,
out-of-range value, or unrecognized enum returns a failed verdict.

The schema is domain-neutral — the caller passes their own Pydantic model
class at construction time.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class OutputVerdict(BaseModel, Generic[SchemaT]):
    valid: bool
    reason: str | None = None
    parsed: SchemaT | None = None


class OutputGuardrail(Generic[SchemaT]):
    """Validates LLM JSON output against a caller-supplied Pydantic schema.

    Example:
        class MyDiagnosisPayload(BaseModel):
            summary: str
            confidence: float

        guardrail = OutputGuardrail(schema=MyDiagnosisPayload)
        verdict = guardrail.validate({"summary": "...", "confidence": 0.9})
        if verdict.valid:
            use(verdict.parsed)
    """

    def __init__(self, *, schema: type[SchemaT]) -> None:
        self._schema = schema

    def validate(self, payload: dict[str, Any]) -> OutputVerdict[SchemaT]:
        """Validate `payload` against the configured schema."""
        try:
            parsed = self._schema.model_validate(payload)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {"msg": str(exc)}
            reason = (
                f"schema_violation: {'.'.join(str(x) for x in first.get('loc', ()))} "
                f"— {first.get('msg', 'unknown')}"
            )
            return OutputVerdict(valid=False, reason=reason)
        return OutputVerdict(valid=True, parsed=parsed)
