"""Speech-plan derivation tests.

Two contracts from the spec drive these tests:

1. The speech stage MUST NOT mutate the approved editorial text —
   :func:`assert_semantic_identity` rejects anything that drifts away
   under :func:`normalize_spoken`.
2. Cue blocks MUST cover the spoken text monotonically with stable ids,
   paragraph_ids that trace the approved structure, and provider
   metadata that is either aligned to existing cues or silently dropped
   (never silently ignored when misaligned).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from studio.content.speech import (
    SemanticMutation,
    SpeechService,
    UnalignedMetadata,
    assert_semantic_identity,
    normalize_spoken,
)
from studio.providers.fake import FakeModelProvider
from studio.schemas import ApprovedScript, CueBlock, PronunciationHint, SpeechPlan

# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2026, 8, 18, tzinfo=UTC)


def _approved(
    *,
    editorial_text: str = (
        "海水为什么是咸的？河流一直在注入淡水。\n\n"
        "答案在岩石风化。雨水把矿物离子冲进河流，河流再把它们送进海洋。"
    ),
    structure: list[str] | None = None,
) -> ApprovedScript:
    if structure is None:
        structure = ["p1", "p2"]
    return ApprovedScript(
        id="approved-1",
        draft_revision_id="d1",
        editorial_text=editorial_text,
        structure=structure,
        fact_card_ids=[],
        approved_at=_now(),
    )


class _MetadataOutput(BaseModel):
    hints: list[PronunciationHint]


def _build_provider(plan_cues: list[CueBlock]) -> FakeModelProvider:
    """Queue a one-hint-per-cue response so the default ``build`` works.

    Empty-string phonetic + ``emphasis='weak'`` exercises the cleanup
    path that drops empty phonetics; no cue is left without a hint.
    """

    provider = FakeModelProvider()
    provider.queue(
        "speech_metadata",
        _MetadataOutput(
            hints=[
                PronunciationHint(
                    cue_id=cue.id,
                    phonetic="",
                    emphasis="weak",
                    pause_ms_before=0,
                )
                for cue in plan_cues
            ]
        ),
    )
    return provider


@pytest.fixture
def approved() -> ApprovedScript:
    return _approved()


@pytest.fixture
def plan(approved: ApprovedScript) -> SpeechPlan:
    # Build once with an empty provider so tests that only need the
    # structural fields don't need bespoke fixtures.
    return SpeechService().build(approved)


# ---------------------------------------------------------------------------
# verbatim-from-brief tests
# ---------------------------------------------------------------------------


def test_speech_plan_preserves_normalized_words(approved: ApprovedScript) -> None:
    service = SpeechService()
    plan = service.build(approved)
    assert normalize_spoken(plan.spoken_text) == normalize_spoken(
        approved.editorial_text
    )
    # Brief uses approved.revision_id; the ApprovedScript carries the
    # revision lineage via its own ``id`` (the artifact id of the frozen
    # approved script) — that is what the plan anchors to.
    assert plan.source_revision_id == approved.id


def test_added_fact_is_rejected(approved: ApprovedScript) -> None:
    # An attacker / model that rewrites the spoken text with a fresh
    # number must fail the semantic identity check.
    plan = SpeechService().build(approved)
    tampered = plan.model_copy(
        update={
            "spoken_text": approved.editorial_text + " 而且 2026 年人均盐摄入 12 克。"
        }
    )
    with pytest.raises(SemanticMutation):
        assert_semantic_identity(approved, tampered)


# ---------------------------------------------------------------------------
# segmentation coverage
# ---------------------------------------------------------------------------


def test_cue_blocks_cover_spoken_text_monotonically(plan: SpeechPlan) -> None:
    """Every char 0..len(spoken_text) is in exactly one cue, in order."""

    n = len(plan.spoken_text)
    expected_next = 0
    for block in plan.cue_blocks:
        assert block.char_start == expected_next, (
            f"block {block.index} starts at {block.char_start}, expected {expected_next}"
        )
        assert block.char_end > block.char_start, (
            f"block {block.index} has non-positive length"
        )
        assert block.char_end <= n, (
            f"block {block.index} ends at {block.char_end}, past end of text ({n})"
        )
        assert (
            plan.spoken_text[block.char_start : block.char_end] == block.text
        ), f"block {block.index} text does not match slice"
        expected_next = block.char_end
    assert expected_next == n, (
        f"blocks covered {expected_next} chars but spoken_text is {n} chars"
    )


def test_cue_block_ids_are_stable_across_runs(approved: ApprovedScript) -> None:
    plan_a = SpeechService().build(approved)
    plan_b = SpeechService().build(approved)
    ids_a = [b.id for b in plan_a.cue_blocks]
    ids_b = [b.id for b in plan_b.cue_blocks]
    assert ids_a == ids_b
    assert all(ids_a)


def test_cue_block_paragraph_ids_trace_approved_structure() -> None:
    approved = _approved(
        editorial_text=(
            "第一段第一句。第一段第二句。\n\n"
            "第二段内容。\n\n"
            "第三段开场。第三段收尾。"
        ),
        structure=["p1", "p2", "p3"],
    )
    plan = SpeechService().build(approved)
    paragraph_ids = [block.paragraph_id for block in plan.cue_blocks]

    # Each paragraph gets at least one cue; order matches the structure.
    assert paragraph_ids[0] == "p1"
    assert paragraph_ids[-1] == "p3"
    assert "p1" in paragraph_ids
    assert "p2" in paragraph_ids
    assert "p3" in paragraph_ids
    # Blocks for p1 come before blocks for p2 come before blocks for p3.
    last_p1 = max(i for i, pid in enumerate(paragraph_ids) if pid == "p1")
    first_p2 = min(i for i, pid in enumerate(paragraph_ids) if pid == "p2")
    last_p2 = max(i for i, pid in enumerate(paragraph_ids) if pid == "p2")
    first_p3 = min(i for i, pid in enumerate(paragraph_ids) if pid == "p3")
    assert last_p1 < first_p2 <= last_p2 < first_p3


# ---------------------------------------------------------------------------
# provider metadata
# ---------------------------------------------------------------------------


def test_provider_metadata_must_align_with_cues(approved: ApprovedScript) -> None:
    provider = FakeModelProvider()
    provider.queue(
        "speech_metadata",
        _MetadataOutput(
            hints=[
                PronunciationHint(cue_id="definitely-not-a-real-cue"),
            ]
        ),
    )
    service = SpeechService(provider)
    with pytest.raises(UnalignedMetadata):
        service.build(approved)


def test_provider_metadata_negative_pause_dropped(approved: ApprovedScript) -> None:
    plan = SpeechService().build(approved)
    first_cue = plan.cue_blocks[0]
    provider = FakeModelProvider()
    provider.queue(
        "speech_metadata",
        _MetadataOutput(
            hints=[
                PronunciationHint(
                    cue_id=first_cue.id,
                    phonetic="shui3",
                    emphasis="strong",
                    pause_ms_before=-5,
                ),
            ]
        ),
    )
    new_plan = SpeechService(provider).build(approved)
    assert first_cue.id not in new_plan.pause_ms
    # The non-negative fields survive.
    assert first_cue.id in new_plan.emphasis


def test_unaligned_metadata_error_names_cue_id(approved: ApprovedScript) -> None:
    provider = FakeModelProvider()
    provider.queue(
        "speech_metadata",
        _MetadataOutput(
            hints=[PronunciationHint(cue_id="unknown-cue-id")],
        ),
    )
    service = SpeechService(provider)
    with pytest.raises(UnalignedMetadata, match="unknown-cue-id"):
        service.build(approved)


# ---------------------------------------------------------------------------
# duration & provider-less build
# ---------------------------------------------------------------------------


def test_duration_sec_is_char_count_over_rate() -> None:
    """60 chars at 4 cps → exactly 15.0s."""

    text = "一" * 60  # 60 chars, no sentence-ending punct -> single block
    approved = _approved(
        editorial_text=text,
        structure=["p1"],
    )
    plan = SpeechService().build(approved)
    assert plan.duration_sec == 15.0
    # No string joins, no whitespace added: total chars == len(text).
    assert sum(len(b.text) for b in plan.cue_blocks) == 60


def test_build_works_without_provider(approved: ApprovedScript) -> None:
    plan = SpeechService(provider=None).build(approved)
    assert plan.pronunciation_hints == []
    assert plan.emphasis == []
    assert plan.pause_ms == {}
    assert plan.cue_blocks  # segmentation still happened


# ---------------------------------------------------------------------------
# normalise_spoken unit tests
# ---------------------------------------------------------------------------


def test_normalize_spoken_collapses_whitespace_and_strips_nbsp() -> None:
    raw = "Hello 　  world\n\nfoo\tbar"
    expected = "hello world foo bar"
    assert normalize_spoken(raw) == expected


def test_normalize_spoken_keeps_punctuation_and_digits() -> None:
    raw = "在 2026 年，比例为 3.14%。重要！?"
    expected = "在 2026 年，比例为 3.14%。重要！?"
    assert normalize_spoken(raw) == expected


# ---------------------------------------------------------------------------
# happy-path build with metadata
# ---------------------------------------------------------------------------


def test_build_with_provider_returns_structured_plan(approved: ApprovedScript) -> None:
    plan = SpeechService().build(approved)
    provider = _build_provider(plan.cue_blocks)
    result = SpeechService(provider).build(approved)

    assert result.cue_blocks  # deterministic blocks present
    # Every cue got a hint; the empty phonetic got cleaned to None.
    assert len(result.pronunciation_hints) == len(result.cue_blocks)
    assert all(hint.phonetic is None for hint in result.pronunciation_hints)
    assert all(hint.emphasis == "weak" for hint in result.pronunciation_hints)
    # pause_ms_before == 0 is kept (zero is a valid pause).
    for hint in result.pronunciation_hints:
        assert hint.cue_id in result.pause_ms
        assert result.pause_ms[hint.cue_id] == 0


def test_assert_semantic_identity_passes_for_fresh_plan(
    approved: ApprovedScript,
) -> None:
    plan = SpeechService().build(approved)
    assert_semantic_identity(approved, plan)  # does not raise


def test_assert_semantic_identity_message_contains_diff(
    approved: ApprovedScript,
) -> None:
    plan = SpeechService().build(approved)
    tampered = plan.model_copy(
        update={"spoken_text": approved.editorial_text + " 多余的一句话。"}
    )
    with pytest.raises(SemanticMutation, match="added"):
        assert_semantic_identity(approved, tampered)