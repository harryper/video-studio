"""Payload validation dispatch plus shared Pydantic schemas.

The ``register`` / ``validate_payload`` helpers underpin the artifact
repository (Task 2). Pydantic models for Content Studio's structured content
live here too so Tasks 5+ can import them from a single place without
inventing new modules for each task.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

_VALIDATORS: dict[str, Any] = {}


def register(kind: str) -> Any:
    """Decorator: register ``validator`` for ``kind`` payloads."""

    def decorator(validator: Any) -> Any:
        _VALIDATORS[kind] = validator
        return validator

    return decorator


def validate_payload(kind: str, payload: Any) -> dict[str, Any]:
    """Validate ``payload`` for the given ``kind``.

    Returns a normalised dict. Unknown kinds pass through untouched (Task 2
    rule: do NOT block unknown kinds).
    """

    if not isinstance(payload, dict):
        raise ValueError(
            f"artifact payload for kind {kind!r} must be a dict, got {type(payload).__name__}"
        )
    validator = _VALIDATORS.get(kind)
    if validator is None:
        return dict(payload)
    result = validator(payload)
    if isinstance(result, dict):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump()
    return dict(result)


class TopicDiagnosis(BaseModel):
    """Stub schema used by Task 4.

    Task 5 extends this with ``audience_prior_knowledge``, ``central_tension``,
    ``misconceptions``, ``scope`` and ``excluded_topics``. For Task 4 only
    ``core_question`` is needed so providers can wire their contract tests.
    """

    core_question: str


class SourceDocument(BaseModel):
    """A search result returned by :class:`SearchProvider`."""

    title: str
    url: str
    snippet: str
    publisher: str
    published_at: datetime | None = None


__all__ = [
    "SourceDocument",
    "TopicDiagnosis",
    "register",
    "validate_payload",
]
