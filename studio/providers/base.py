"""Abstract provider interfaces and shared redaction helpers.

Both adapters wrap a module-level ``logging.getLogger(__name__)``. Anything
logged that contains a request body, response body, or sensitive header must
flow through :func:`redact_body` / :func:`redact_headers` so secrets never
leak into the application logs.
"""

from __future__ import annotations

import abc
import logging
import re
from typing import Any, TypeVar

from pydantic import BaseModel

from studio.schemas import SourceDocument

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_SENSITIVE_KEY = re.compile(
    r"^(authorization|api[_-]?key|token|password|secret)$", re.IGNORECASE
)
_HEADER_DROP = frozenset({"authorization", "x-api-key", "cookie"})


class ModelProviderError(Exception):
    """Raised when a model provider cannot produce a valid response."""


def _is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY.match(key))


def redact_body(body: Any) -> Any:
    """Return a copy of ``body`` with sensitive field values replaced by ``[REDACTED]``.

    Sensitive keys (case-insensitive): ``authorization``, ``api_key`` /
    ``api-key`` / ``apikey``, ``token``, ``password``, ``secret``.
    """

    if isinstance(body, dict):
        return {
            k: ("[REDACTED]" if _is_sensitive_key(k) else redact_body(v))
            for k, v in body.items()
        }
    if isinstance(body, list):
        return [redact_body(item) for item in body]
    if isinstance(body, tuple):
        return tuple(redact_body(item) for item in body)
    return body


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Return a copy of ``headers`` with sensitive entries removed entirely."""

    if not headers:
        return {}
    return {
        k: v for k, v in headers.items() if k.lower() not in _HEADER_DROP
    }


class ModelProvider[T: BaseModel](abc.ABC):
    """Adapter that converts a model call into a parsed Pydantic model."""

    @abc.abstractmethod
    def generate(
        self,
        schema: type[T],
        system: str,
        prompt: str,
        *,
        operation: str,
    ) -> T:
        """Call the underlying model and return a validated ``schema`` instance."""


class SearchProvider(abc.ABC):
    """Adapter that converts a search query into a list of source documents."""

    @abc.abstractmethod
    def search(self, query: str, *, limit: int = 5) -> list[SourceDocument]:
        """Return up to ``limit`` source documents for ``query``."""


__all__ = [
    "ModelProvider",
    "ModelProviderError",
    "SearchProvider",
    "redact_body",
    "redact_headers",
]
