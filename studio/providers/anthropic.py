"""Adapter for the official ``anthropic`` SDK with one-shot schema repair.

The adapter parses the model's response into the requested Pydantic schema.
If the first response is not valid JSON (or does not match the schema), it
issues one repair call whose metadata adds ``mode="schema_repair"`` and
re-states the original prompt plus the invalid payload. If the repair still
fails, the adapter raises :class:`ModelProviderError` — never a third attempt.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from studio.providers.base import (
    ModelProvider,
    ModelProviderError,
    T,
    redact_body,
)

logger = logging.getLogger(__name__)


class AnthropicProvider(ModelProvider[BaseModel]):
    """Adapter around an ``anthropic.Anthropic``-shaped client.

    ``client`` is duck-typed: any object exposing a ``messages`` attribute
    whose ``create(**kwargs)`` returns an object with ``.content[0].text``
    satisfies this contract. The official ``anthropic`` SDK and our test
    double both satisfy this shape.
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str = "claude-sonnet-4-5",
        max_tokens: int = 4096,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens

    def generate(
        self,
        schema: type[T],
        system: str,
        prompt: str,
        *,
        operation: str,
    ) -> T:
        base_messages = [{"role": "user", "content": prompt}]
        base_metadata = {"operation": operation}
        first_text = self._invoke(system, base_messages, base_metadata)
        parsed, first_errors = self._parse(first_text, schema)
        if parsed is not None:
            return parsed

        repair_metadata = {**base_metadata, "mode": "schema_repair"}
        error_summary = self._format_validation_errors(first_errors)
        repair_messages = base_messages + [
            {
                "role": "user",
                "content": (
                    "Your previous response did not satisfy the required "
                    "schema. Reply with ONLY a JSON object that matches the "
                    f"schema. Previous response: {first_text!r}"
                    + (f"\nValidation errors: {error_summary}" if error_summary else "")
                ),
            }
        ]
        second_text = self._invoke(system, repair_messages, repair_metadata)
        parsed, _ = self._parse(second_text, schema)
        if parsed is None:
            logger.error(
                "anthropic schema repair failed for operation=%r", operation
            )
            raise ModelProviderError(
                f"anthropic schema repair failed for operation {operation!r}"
            )
        return parsed

    # ------------------------------------------------------------------
    def _invoke(
        self,
        system: str,
        messages: list[dict[str, str]],
        metadata: dict[str, str],
    ) -> str:
        try:
            response = self._client.messages.create(
                model=self._model,
                system=system,
                max_tokens=self._max_tokens,
                messages=messages,
                metadata=metadata,
            )
        except Exception as exc:
            # Never include the raw exception text — it may carry secrets.
            logger.error(
                "anthropic provider call failed: operation=%s error=%s metadata=%s",
                metadata.get("operation", "?"),
                exc.__class__.__name__,
                redact_body(metadata),
            )
            raise ModelProviderError(
                f"anthropic provider call failed for operation "
                f"{metadata.get('operation', '?')!r}"
            ) from exc
        return self._extract_text(response)

    @staticmethod
    def _extract_text(response: Any) -> str:
        content = getattr(response, "content", None) or []
        if not content:
            return ""
        first = content[0]
        return getattr(first, "text", str(first))

    @staticmethod
    def _parse(
        text: str, schema: type[T]
    ) -> tuple[T | None, ValidationError | None]:
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        try:
            return schema.model_validate(data), None
        except ValidationError as exc:
            return None, exc

    @staticmethod
    def _format_validation_errors(errors: ValidationError | None) -> str:
        if errors is None:
            return ""
        try:
            items = errors.errors(include_url=False)
        except Exception:  # pragma: no cover - defensive
            return ""
        return "; ".join(
            ".".join(str(p) for p in err.get("loc", ())) + ": " + err.get("msg", "")
            for err in items
        )[:1024]


__all__ = ["AnthropicProvider"]
