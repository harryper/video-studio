"""Deterministic in-memory :class:`ModelProvider` for tests and offline runs.

The fixture queue is consumed in order: :meth:`FakeModelProvider.generate`
returns the head of the list (a deep copy) and removes it from the queue.
Once a queue is empty, calls raise :class:`ModelProviderError` naming the
operation so the gap is easy to spot in test failures.

The returned object is validated against the requested ``schema`` so the
caller gets a Pydantic model instance (the same shape the real providers
return after their parse step). This means the fixture file can hold a
plain dict — the JSON-typed contract is enforced here.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from studio.providers.base import ModelProvider, ModelProviderError

logger = logging.getLogger(__name__)


class FakeModelProvider(ModelProvider[BaseModel]):
    """Pops pre-canned responses keyed by ``operation`` name."""

    def __init__(
        self, responses: dict[str, list[BaseModel]] | None = None
    ) -> None:
        self.responses: dict[str, list[BaseModel]] = {}
        if responses:
            for operation, items in responses.items():
                self.responses[operation] = list(items)

    def queue(self, operation: str, *items: Any) -> None:
        """Append ``items`` to the queue for ``operation``."""

        self.responses.setdefault(operation, []).extend(items)

    def record(self, path: str | Path, operation: str) -> None:
        """Load a JSON fixture file and queue its ``responses`` for ``operation``.

        The fixture is a JSON object with a ``"responses"`` list — each
        entry is queued in order so the next ``generate(operation=...)``
        returns the next item. See ``tests/fixtures/provider_responses/``
        for the recorded valid + invalid fixtures.
        """

        import json

        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        responses = payload.get("responses", [])
        self.responses.setdefault(operation, []).extend(responses)

    def generate(
        self,
        schema: type[BaseModel],
        system: str,
        prompt: str,
        *,
        operation: str,
    ) -> BaseModel:
        queue = self.responses.get(operation)
        if not queue:
            logger.error(
                "FakeModelProvider has no fixture for operation=%r", operation
            )
            raise ModelProviderError(
                f"FakeModelProvider has no fixture queued for operation {operation!r}"
            )
        fixture = queue.pop(0)
        if isinstance(fixture, BaseModel):
            return copy.deepcopy(fixture)
        # Validate the dict against the requested schema so the caller
        # gets a typed model instance (the same shape the real
        # providers hand back after their parse step).
        return schema.model_validate(fixture)


__all__ = ["FakeModelProvider"]
