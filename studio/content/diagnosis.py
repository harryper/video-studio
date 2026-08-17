"""Topic diagnosis service.

The diagnosis stage is intentionally narrow: it asks the model ONLY
for the central investigative question, the audience's prior
knowledge, the central tension, the misconceptions the script should
address, and the in-scope / out-of-scope subtopics. It must NOT
pre-decide how the script opens, what the hook reads like, or what
the conclusion does — those are later stages' jobs.

Decoupling the diagnosis from the draft prevents the obvious failure
mode where the opening line is silently chosen by a single prompt and
the rest of the script optimizes itself to defend that line. By
forcing each stage to operate on a typed contract, downstream stages
can only see what the previous stage promised.
"""

from __future__ import annotations

from studio.providers.base import ModelProvider
from studio.schemas import TopicDiagnosis

DIAGNOSIS_SYSTEM = """你是科普短视频选题诊断助手。你只回答关于"这个题材是什么、核心张力在哪里、观众已经知道什么"的诊断问题。

严禁在响应中包含任何钩子（hook）、开场白、结论（conclusion）、叙事结构（narrative/structure）或脚本片段。诊断阶段不写稿，只界定范围。

响应必须是严格符合 schema 的 JSON 对象，字段：
- core_question: 这一集要回答的核心问题（一句话）。
- audience_prior_knowledge: 目标观众已经知道什么（一到两句）。
- central_tension: 这个题材的核心矛盾或悬念（一句话）。这是后续文案必须兑现的张力。
- misconceptions: 观众最可能持有的常见误解，字符串列表。
- scope: 本期内容将覆盖的子主题，字符串列表。
- excluded_topics: 本期内容明确不涉及的子主题，字符串列表。"""


def diagnose_topic(topic: str, provider: ModelProvider) -> TopicDiagnosis:
    """Ask ``provider`` to diagnose ``topic`` and return a ``TopicDiagnosis``.

    The system prompt forbids draft hooks / conclusions / structure so the
    diagnosis stage cannot pre-empt the writer's job. The operation key
    on the provider is ``"diagnosis"`` — downstream caches and rate
    limiters key off this string.
    """

    return provider.generate(
        TopicDiagnosis,
        DIAGNOSIS_SYSTEM,
        f"主题：{topic}",
        operation="diagnosis",
    )


__all__ = ["DIAGNOSIS_SYSTEM", "diagnose_topic"]
