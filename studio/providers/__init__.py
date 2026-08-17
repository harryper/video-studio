"""External provider adapters for Content Studio.

Boundary classes that separate Content Studio business logic from external
model and search APIs. Tasks 5+ depend on the abstract :class:`ModelProvider`
and :class:`SearchProvider` types defined here.
"""

from studio.providers.anthropic import AnthropicProvider
from studio.providers.base import (
    ModelProvider,
    ModelProviderError,
    SearchProvider,
    redact_body,
    redact_headers,
)
from studio.providers.fake import FakeModelProvider
from studio.providers.search import HttpSearchProvider

__all__ = [
    "AnthropicProvider",
    "FakeModelProvider",
    "HttpSearchProvider",
    "ModelProvider",
    "ModelProviderError",
    "SearchProvider",
    "redact_body",
    "redact_headers",
]
