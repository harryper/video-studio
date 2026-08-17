"""HTTP search adapter for Content Studio.

Calls the configured JSON endpoint with ``GET {url}?q={query}&limit={limit}``
and normalises the response into :class:`~studio.schemas.SourceDocument`
instances. An optional bearer token is sent in the ``Authorization`` header;
error logs strip that header automatically.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from studio.config import Settings
from studio.providers.base import (
    ModelProviderError,
    SearchProvider,
    redact_headers,
)
from studio.schemas import SourceDocument

logger = logging.getLogger(__name__)


class HttpSearchProvider(SearchProvider):
    """Calls the configured ``Settings.search_provider_url`` JSON endpoint."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        self._client: Any = client or httpx.Client()

    def search(self, query: str, *, limit: int = 5) -> list[SourceDocument]:
        url = self._settings.search_provider_url
        if not url:
            raise ModelProviderError("search_provider_url is not configured")
        headers = self._auth_headers()
        try:
            response = self._client.get(
                url,
                params={"q": query, "limit": limit},
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.error(
                "search provider call failed: query=%r limit=%d headers=%s",
                query,
                limit,
                redact_headers(headers),
            )
            raise ModelProviderError(
                f"search provider error: {exc.__class__.__name__}"
            ) from exc

        if not isinstance(data, dict):
            return []
        results = data.get("results")
        if not results:
            return []
        return [SourceDocument.model_validate(item) for item in results]

    def _auth_headers(self) -> dict[str, str]:
        token = self._settings.search_provider_token
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


__all__ = ["HttpSearchProvider"]
