"""Topic diagnosis service tests.

The diagnosis stage produces a ``TopicDiagnosis`` that downstream stages
(pitches, narrative, draft) build on. These tests pin the contract:

* All six fields are populated and well-typed.
* The system prompt forbids draft hooks / conclusions / structure — the
  model is asked ONLY to answer "what is this topic about and where is
  the tension", not "how should we open the script".
* The model receives a ``core_question`` slot to fill; bad payloads cause
  ``ModelProviderError`` after the one-shot repair (matching Task 4's
  contract).
"""

from __future__ import annotations

from typing import Any

import pytest

from studio.content.diagnosis import diagnose_topic
from studio.providers.base import ModelProviderError
from studio.providers.fake import FakeModelProvider
from studio.schemas import TopicDiagnosis


class _RecordingProvider(FakeModelProvider):
    """Fake provider that also records every ``generate`` call.

    ``FakeModelProvider.generate`` returns the next fixture for ``operation``
    from the queue. The diagnosis tests need to assert on the system
    prompt that was sent so we record every call here. This wrapper adds
    a ``calls`` list and forwards everything else to the parent.
    """

    def __init__(self, responses: dict[str, list[Any]] | None = None) -> None:
        super().__init__(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self,
        schema: Any,
        system: str,
        prompt: str,
        *,
        operation: str,
    ) -> Any:
        self.calls.append(
            {
                "schema": schema,
                "system": system,
                "prompt": prompt,
                "operation": operation,
            }
        )
        return super().generate(schema, system, prompt, operation=operation)


def test_diagnosis_returns_six_fields() -> None:
    """``diagnose_topic`` populates every required field."""

    diagnosis = TopicDiagnosis(
        core_question="为什么海水是咸的？",
        audience_prior_knowledge="普通观众，知道有海洋但不熟悉化学",
        central_tension="为什么河流入海却不把盐冲淡",
        misconceptions=["海水=溶解的食用盐", "海洋一直这么咸"],
        scope=["盐的来源", "盐度平衡", "与其他星球的对比"],
        excluded_topics=["海洋化学实验教学", "深海生物适应"],
    )
    provider = _RecordingProvider({"diagnosis": [diagnosis]})

    result = diagnose_topic("海水为什么是咸的", provider)

    assert isinstance(result, TopicDiagnosis)
    assert result.core_question == "为什么海水是咸的？"
    assert result.audience_prior_knowledge == "普通观众，知道有海洋但不熟悉化学"
    assert result.central_tension == "为什么河流入海却不把盐冲淡"
    assert len(result.misconceptions) == 2
    assert len(result.scope) == 3
    assert len(result.excluded_topics) == 2


def test_diagnosis_contains_no_draft_hook() -> None:
    """The system prompt forbids draft hooks / conclusions / structure.

    The diagnosis stage must NOT pre-decide how the script opens, because
    that is later stages' job. The prompt must explicitly tell the model
    hooks / conclusions / structure are off-limits — verified via the
    captured call's ``system`` argument. The check looks for a
    prohibition verb (e.g. ``禁止`` / ``forbid``) co-located with the
    concept being forbidden.
    """

    diagnosis = TopicDiagnosis(
        core_question="核心问题",
        audience_prior_knowledge="观众背景",
        central_tension="核心张力",
        misconceptions=["误解 1"],
        scope=["子主题"],
        excluded_topics=["排除"],
    )
    provider = _RecordingProvider({"diagnosis": [diagnosis]})

    diagnose_topic("任何主题", provider)

    assert len(provider.calls) == 1
    system = provider.calls[0]["system"]
    lowered = system.lower()
    # The system prompt must contain a prohibition verb (Chinese
    # ``禁止`` / ``严禁`` or English ``forbid`` / ``do not`` / ``must not``).
    prohibit_verbs = ("禁止", "严禁", "forbid", "do not", "must not")
    assert any(verb.lower() in lowered for verb in prohibit_verbs), (
        f"system prompt must contain a prohibition verb (one of "
        f"{prohibit_verbs!r}); got: {system!r}"
    )
    # The prohibition must cover the things later draft stages own.
    forbidden_concepts = (
        "hook",
        "钩子",
        "conclusion",
        "结论",
        "structure",
        "结构",
        "narrative",
        "叙事",
    )
    assert any(concept.lower() in lowered for concept in forbidden_concepts), (
        f"system prompt must name at least one forbidden concept (one of "
        f"{forbidden_concepts!r}); got: {system!r}"
    )


def test_diagnosis_scope_and_excluded_topics_are_lists() -> None:
    """``scope`` and ``excluded_topics`` must come back as lists."""

    diagnosis = TopicDiagnosis(
        core_question="核心问题",
        audience_prior_knowledge="观众背景",
        central_tension="核心张力",
        misconceptions=["m"],
        scope=["子主题 A", "子主题 B"],
        excluded_topics=["排除 A"],
    )
    provider = _RecordingProvider({"diagnosis": [diagnosis]})

    result = diagnose_topic("主题", provider)

    assert isinstance(result.scope, list)
    assert isinstance(result.excluded_topics, list)
    assert all(isinstance(x, str) for x in result.scope)
    assert all(isinstance(x, str) for x in result.excluded_topics)


def test_diagnosis_rejects_topic_missing_central_question() -> None:
    """Model returns garbage → ``ModelProviderError`` after repair.

    When the model fails to fill ``core_question`` even after the
    one-shot repair (per Task 4's contract), the call must raise
    ``ModelProviderError`` so the worker can record a clean failure
    instead of handing a shape-less diagnosis downstream.
    """

    class _BadProvider(FakeModelProvider):
        def __init__(self) -> None:
            super().__init__({"diagnosis": []})
            self.failures = 0

        def generate(
            self,
            schema: Any,
            system: str,
            prompt: str,
            *,
            operation: str,
        ) -> Any:
            self.failures += 1
            raise ModelProviderError(
                f"operation {operation!r} produced no core_question"
            )

    provider = _BadProvider()
    with pytest.raises(ModelProviderError):
        diagnose_topic("主题", provider)
    assert provider.failures == 1
