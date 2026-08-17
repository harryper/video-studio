"""Speech-plan derivation: project an :class:`ApprovedScript` onto a
:class:`SpeechPlan` without mutating the editorial text.

Two responsibilities live here:

* :class:`SpeechService` — turn an approved script into a :class:`SpeechPlan`
  whose ``cue_blocks`` are a deterministic, content-only segmentation of the
  approved text. Provider output is permitted ONLY for pronunciation,
  emphasis, and pause metadata keyed to existing :class:`CueBlock` ids;
  hints that fall outside the plan raise :class:`UnalignedMetadata`,
  hints that are nonsensical (e.g. negative pause) are silently dropped.
* :func:`assert_semantic_identity` — module-level guard that raises
  :class:`SemanticMutation` when ``plan.spoken_text`` differs from the
  approved editorial text under :func:`normalize_spoken`. Downstream
  TTS/alignment must call this before consuming the plan.

The spec rule (Section 8) is absolute: the speech stage never mutates
editorial text. Segmentation is computed from punctuation/syntax
alone, and duration is a function of character count and a configured
voice rate.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from studio.providers.base import ModelProvider
from studio.schemas import (
    ApprovedScript,
    CueBlock,
    PronunciationHint,
    SpeechPlan,
)

SPEECH_SYSTEM = """你是科普短视频的口播元数据助理。你只能基于已确定的字幕语义块生成发音、重音和停顿信息，绝不能修改、改写或补全任何文字内容。

严禁做以下事情：
- 改写、删除、添加任何字幕块中的文字（即使是为了"更顺口"）。
- 修改事实、数字、专有名词或引文。
- 输出超出输入 cue_id 范围的发音或重音提示。

每个 hint 必须关联到一个已有的 cue_id；phonetic 用音节串（不含声调数字），emphasis 取值 strong 或 weak，pause_ms_before 取非负整数表示该 cue 之前的停顿毫秒数。

响应必须是严格符合 schema 的 JSON 对象，字段：hints 列表，每项包含 cue_id 及可选的 phonetic / emphasis / pause_ms_before。"""


class SemanticMutation(Exception):
    """Raised when a :class:`SpeechPlan` mutates the approved editorial text.

    The message carries the offending substring diff so reviewers can
    locate the exact addition / removal / replacement without diffing
    the two documents by hand.
    """


class UnalignedMetadata(Exception):
    """Raised when the provider returns metadata keyed to an unknown cue."""

    def __init__(self, cue_id: str) -> None:
        super().__init__(
            f"provider returned metadata for unknown cue_id {cue_id!r}"
        )
        self.cue_id = cue_id


# Sentence-ending punctuation that marks a cue boundary. Includes the
# full-width Chinese terminal punctuation set, ASCII period, question
# mark, exclamation mark, and the paragraph-join newline (so a
# paragraph that lacks punctuation still splits at the blank line).
_END_PUNCT = frozenset("。！？?!\n")

DEFAULT_VOICE_RATE_CHARS_PER_SEC = 4.0


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------


def normalize_spoken(text: str) -> str:
    """Equality form used by :func:`assert_semantic_identity`.

    Lowercases, strips ASCII and full-width whitespace (including NBSP
    ``\\u00a0`` and the full-width space ``\\u3000``), and collapses
    runs of whitespace into a single space. Punctuation, digits, and
    Chinese characters are preserved — only whitespace is touched.
    """

    lowered = text.lower()
    stripped_chars = []
    for ch in lowered:
        if ch.isspace():
            stripped_chars.append(" ")
            continue
        stripped_chars.append(ch)
    collapsed = "".join(stripped_chars)
    # Collapse runs of spaces into one; trim leading/trailing spaces.
    return " ".join(collapsed.split())


def _block_id(text: str) -> str:
    """Stable 12-character hash used as the :class:`CueBlock` id."""

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _paragraph_spans(
    editorial_text: str, structure: list[str]
) -> list[tuple[str, int, int]]:
    """Map each entry in ``structure`` to its ``(start, end)`` in ``editorial_text``.

    The approved editorial text is paragraphs joined by ``"\\n\\n"``; the
    service trusts the count to match ``len(structure)``. The returned
    spans use the half-open convention ``[start, end)``.
    """

    parts = editorial_text.split("\n\n")
    if len(parts) != len(structure):
        raise ValueError(
            f"approved editorial_text splits into {len(parts)} paragraphs "
            f"but structure has {len(structure)} entries"
        )
    spans: list[tuple[str, int, int]] = []
    cursor = 0
    for paragraph_id, text in zip(structure, parts):
        start = cursor
        end = cursor + len(text)
        spans.append((paragraph_id, start, end))
        cursor = end + len("\n\n")
    return spans


def _paragraph_id_for_position(
    char_start: int, spans: list[tuple[str, int, int]]
) -> str:
    """Return the paragraph id that ``char_start`` logically belongs to.

    Treats the leading ``"\\n\\n"`` separator of every non-first paragraph
    as part of that paragraph, so a cue whose ``char_start`` falls inside
    the join is attributed to the paragraph it opens.
    """

    if not spans:
        raise ValueError("spans must not be empty")
    # First paragraph: nothing precedes it.
    if char_start < spans[0][2]:
        return spans[0][0]
    # Subsequent paragraphs: leading "\n\n" belongs to the paragraph it opens.
    for i in range(1, len(spans)):
        if char_start < spans[i][2]:
            return spans[i][0]
    # Past the last paragraph — attribute to the last paragraph.
    return spans[-1][0]


# ---------------------------------------------------------------------------
# segmentation
# ---------------------------------------------------------------------------


def _segment_cue_blocks(
    spoken_text: str, spans: list[tuple[str, int, int]]
) -> list[CueBlock]:
    """Deterministic cue-block segmentation of ``spoken_text``.

    A block boundary occurs at the first sentence-ending punctuation
    (``。！？?!\\n``) where the prefix since the previous boundary is
    non-empty. Every character of ``spoken_text`` belongs to exactly one
    block; blocks are returned in document order with stable, hash-based
    ids so downstream stages can key metadata to them across runs.
    Leading sentence-ending punctuation at the start of a candidate
    block (e.g. the ``"\\n\\n"`` join between two paragraphs) is kept
    with the next block rather than dropped, so coverage stays monotonic.
    """

    blocks: list[CueBlock] = []
    n = len(spoken_text)
    if n == 0:
        return blocks

    i = 0
    while i < n:
        block_start = i
        saw_content = False
        j = i
        while j < n:
            ch = spoken_text[j]
            if ch in _END_PUNCT:
                if saw_content:
                    # Boundary at j (inclusive of the punctuation).
                    end_exclusive = j + 1
                    text = spoken_text[block_start:end_exclusive]
                    blocks.append(
                        CueBlock(
                            id=_block_id(text),
                            index=len(blocks),
                            paragraph_id=_paragraph_id_for_position(block_start, spans),
                            text=text,
                            char_start=block_start,
                            char_end=end_exclusive,
                        )
                    )
                    i = end_exclusive
                    break
                # Punctuation without any content since the previous
                # boundary: keep scanning forward so this char stays in
                # the upcoming block (do NOT advance ``block_start``).
                j += 1
                continue
            saw_content = True
            j += 1
        else:
            # Reached end without hitting punctuation — final block runs to end.
            text = spoken_text[block_start:n]
            blocks.append(
                CueBlock(
                    id=_block_id(text),
                    index=len(blocks),
                    paragraph_id=_paragraph_id_for_position(block_start, spans),
                    text=text,
                    char_start=block_start,
                    char_end=n,
                )
            )
            break

    return blocks


# ---------------------------------------------------------------------------
# provider I/O
# ---------------------------------------------------------------------------


class _MetadataDraft(BaseModel):
    """Raw shape the provider is expected to return for ``"speech_metadata"``."""

    hints: list[PronunciationHint]


def _speech_metadata_prompt(spoken_text: str, cues: list[CueBlock]) -> str:
    """User prompt: ask the model for pronunciation hints keyed to cue ids."""

    cue_lines = "\n".join(
        f"- cue_id={cue.id!r} text={cue.text!r}" for cue in cues
    )
    return (
        "请为以下中文字幕语义块生成发音、重音与停顿元数据。不要修改任何文字。\n\n"
        f"口播全文：\n{spoken_text}\n\n"
        f"字幕块（每个 hint 的 cue_id 必须等于下列 cue_id 之一）：\n{cue_lines}\n"
    )


def _collect_metadata(
    spoken_text: str,
    cue_blocks: list[CueBlock],
    provider: ModelProvider | None,
) -> list[PronunciationHint]:
    """Call the provider for pronunciation metadata.

    Provider output is filtered against the cue-id set: hints whose
    ``cue_id`` is unknown raise :class:`UnalignedMetadata`; individual
    nonsensical fields (empty phonetic, negative pause) are silently
    scrubbed. A hint that ends up with no useful field is dropped
    entirely. A ``None`` provider returns an empty hint list so the
    build path stays usable in tests that do not care about metadata.
    """

    if provider is None:
        return []

    raw = provider.generate(
        _MetadataDraft,
        SPEECH_SYSTEM,
        _speech_metadata_prompt(spoken_text, cue_blocks),
        operation="speech_metadata",
    )

    valid_ids = {cue.id for cue in cue_blocks}
    cleaned: list[PronunciationHint] = []
    for hint in raw.hints:
        if hint.cue_id not in valid_ids:
            raise UnalignedMetadata(hint.cue_id)
        phonetic = hint.phonetic
        if phonetic is not None and not phonetic.strip():
            phonetic = None
        pause_ms = hint.pause_ms_before
        if pause_ms is not None and pause_ms < 0:
            pause_ms = None
        # If every meaningful field has been scrubbed, drop the hint.
        if phonetic is None and hint.emphasis is None and pause_ms is None:
            continue
        cleaned.append(
            PronunciationHint(
                cue_id=hint.cue_id,
                phonetic=phonetic,
                emphasis=hint.emphasis,
                pause_ms_before=pause_ms,
            )
        )
    return cleaned


def _split_metadata(
    hints: list[PronunciationHint],
) -> tuple[list[PronunciationHint], list[str], dict[str, int]]:
    """Project the raw hints onto the three derived fields of :class:`SpeechPlan`."""

    emphasis: list[str] = []
    pause_ms: dict[str, int] = {}
    for hint in hints:
        if hint.emphasis == "strong" and hint.cue_id not in emphasis:
            emphasis.append(hint.cue_id)
        if hint.pause_ms_before is not None and hint.cue_id not in pause_ms:
            pause_ms[hint.cue_id] = hint.pause_ms_before
    return list(hints), emphasis, pause_ms


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


class SpeechService:
    """Derive an immutable :class:`SpeechPlan` from an :class:`ApprovedScript`."""

    def __init__(
        self,
        provider: ModelProvider | None = None,
        *,
        voice_rate_chars_per_sec: float = DEFAULT_VOICE_RATE_CHARS_PER_SEC,
    ) -> None:
        self._provider = provider
        self._voice_rate = voice_rate_chars_per_sec

    def build(self, approved: ApprovedScript) -> SpeechPlan:
        """Produce a :class:`SpeechPlan` for ``approved``.

        Deterministic segmentation always runs. The provider is called
        exactly once with ``operation="speech_metadata"`` to gather
        pronunciation / emphasis / pause hints; pass ``provider=None``
        to skip that step (the plan then carries empty hint lists).
        """

        spoken_text = approved.editorial_text
        spans = _paragraph_spans(spoken_text, approved.structure)
        cue_blocks = _segment_cue_blocks(spoken_text, spans)

        total_chars = sum(len(block.text) for block in cue_blocks)
        duration_sec = round(total_chars / self._voice_rate, 2)

        raw_hints = _collect_metadata(spoken_text, cue_blocks, self._provider)
        hints, emphasis, pause_ms = _split_metadata(raw_hints)

        return SpeechPlan(
            id=str(uuid.uuid4()),
            source_revision_id=approved.id,
            editorial_text_source=approved.editorial_text,
            spoken_text=spoken_text,
            duration_sec=duration_sec,
            cue_blocks=cue_blocks,
            pronunciation_hints=hints,
            emphasis=emphasis,
            pause_ms=pause_ms,
            created_at=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# semantic-identity guard
# ---------------------------------------------------------------------------


def assert_semantic_identity(
    approved: ApprovedScript, plan: SpeechPlan
) -> None:
    """Raise :class:`SemanticMutation` if ``plan`` altered the approved text.

    Equality is checked under :func:`normalize_spoken`: case- and
    whitespace-insensitive. The exception message carries the diff
    substring (``"removed: …"`` / ``"added: …"``) so reviewers can
    locate the offending edit without re-comparing the two strings.
    """

    a = normalize_spoken(approved.editorial_text)
    b = normalize_spoken(plan.spoken_text)
    if a == b:
        return

    # Build a small diff hint for the message. Don't aim for a full
    # Myers diff — just expose enough to point at the change.
    a_tokens = a.split()
    b_tokens = b.split()
    if len(b_tokens) > len(a_tokens):
        extra = b_tokens[len(a_tokens):]
        hint = f"added: {' '.join(extra)!r}"
    elif len(a_tokens) > len(b_tokens):
        missing = a_tokens[len(b_tokens):]
        hint = f"removed: {' '.join(missing)!r}"
    else:
        # Same token count — first divergent token is the culprit.
        for left, right in zip(a_tokens, b_tokens):
            if left != right:
                hint = f"replaced {left!r} -> {right!r}"
                break
        else:
            hint = "normalized texts differ but tokens match"
    raise SemanticMutation(hint)


__all__ = [
    "DEFAULT_VOICE_RATE_CHARS_PER_SEC",
    "SPEECH_SYSTEM",
    "SemanticMutation",
    "SpeechService",
    "UnalignedMetadata",
    "assert_semantic_identity",
    "normalize_spoken",
]