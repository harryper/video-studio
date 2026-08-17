"""Deterministic in-memory :class:`ModelProvider` for tests and offline runs.

The fixture queue is consumed in order: :meth:`FakeModelProvider.generate`
returns the head of the list (a deep copy) and removes it from the queue.
Once a queue is empty, calls raise :class:`ModelProviderError` naming the
operation so the gap is easy to spot in test failures.
"""

from __future__ import annotations

import copy
import logging

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

    def queue(self, operation: str, *items: BaseModel) -> None:
        """Append ``items`` to the queue for ``operation``."""

        self.responses.setdefault(operation, []).extend(items)

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
        return copy.deepcopy(fixture)


__all__ = ["FakeModelProvider"]
