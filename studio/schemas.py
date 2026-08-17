"""Payload validation dispatch.

Task 2 only validates the minimal contract that every artifact payload must
satisfy (it must be a dict-like JSON value). Future tasks register concrete
Pydantic models against this dispatcher.
"""

from __future__ import annotations

from typing import Any

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


__all__ = ["register", "validate_payload"]