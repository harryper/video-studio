"""Anti-template repetition fingerprints.

Six fingerprint shapes are extracted from each draft and compared
against the latest approved drafts:

* ``opening_syntax`` — first sentence pattern (catches canned openers
  like "这就有意思了")
* ``transition_distribution`` — counts of connective words (catches
  scripts that always reach for "但是 / 而且 / 然后")
* ``reveal_position`` — where the central tension is exposed (catches
  scripts that always front-load the reveal)
* ``ending_shape`` — last sentence pattern (catches fixed closers
  like "没了")
* ``comparison_patterns`` — kinds of analogies ("就像 / 比如 / 如同")
* ``misconception_correction_pattern`` — how misconceptions are
  surfaced ("很多人以为 / 常被误认为")

If ANY fingerprint similarity exceeds :data:`REPETITION_THRESHOLD` (0.8),
:meth:`analyze_repetition` sets ``must_replan=True``. ``rewrite_suggestions``
is always ``[]`` — the brief forbids synonym-swap rewrites, so the
worker must re-plan rather than patch the existing draft.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from studio.schemas import DraftRevision, RepetitionReport

REPETITION_THRESHOLD = 0.8

_TRANSITION_WORDS = ("但是", "而且", "然后", "于是", "不过", "因此", "其实", "然而")
_COMPARISON_WORDS = ("就像", "比如", "如同", "好比", "像", "类似", "相当于")
_MISCONCEPTION_WORDS = ("很多人以为", "常被误认为", "常被误解", "不少人以为", "许多人以为")

_BANNED_PHRASES = (
    "你以为",
    "这就有意思了",
    "离谱的是",
    "说白了",
    "关键是",
    "没了",
)

_OPENING_PREFIX_LEN = 12
_ENDING_SUFFIX_LEN = 12


@dataclass(frozen=True)
class DraftFingerprints:
    """Six-shape fingerprint vector for one draft.

    ``has_reveal_marker`` distinguishes "the draft deliberately reveals
    at position X" from "the draft has no reveal sentence, defaulting
    to end-of-script". Without the flag, two drafts with no reveal
    marker would both report position 1.0 and falsely look identical.
    """

    opening_syntax: str
    transition_distribution: tuple[tuple[str, int], ...]
    reveal_position: float
    has_reveal_marker: bool
    ending_shape: str
    comparison_patterns: tuple[str, ...]
    misconception_correction_pattern: str

    def similarity(self, other: DraftFingerprints) -> tuple[float, ...]:
        """Return per-shape similarity in [0, 1].

        Tuple order matches :class:`RepetitionReport`'s ``*_similarity``
        fields, so callers can zip them directly. Empty / absent
        features return ``0.0`` (no signal) instead of ``1.0`` (false
        agreement on "we both said nothing") so two unrelated drafts
        do not spuriously trip the threshold.
        """

        return (
            _string_similarity(self.opening_syntax, other.opening_syntax),
            _counter_similarity(self.transition_distribution, other.transition_distribution),
            _position_similarity(
                self.reveal_position, self.has_reveal_marker,
                other.reveal_position, other.has_reveal_marker,
            ),
            _string_similarity(self.ending_shape, other.ending_shape),
            _set_similarity(self.comparison_patterns, other.comparison_patterns),
            _string_similarity(
                self.misconception_correction_pattern,
                other.misconception_correction_pattern,
            ),
        )


def compute_fingerprints(draft: DraftRevision) -> DraftFingerprints:
    """Extract the six-shape fingerprint vector for ``draft``."""

    text = draft.editorial_text
    paragraphs = [p.text for p in draft.paragraphs]
    first_para = paragraphs[0] if paragraphs else text
    last_para = paragraphs[-1] if paragraphs else text

    reveal_pos, has_marker = _reveal_position(text, paragraphs)
    return DraftFingerprints(
        opening_syntax=_first_sentence_prefix(first_para),
        transition_distribution=tuple(
            sorted(Counter(_extract_words(text, _TRANSITION_WORDS)).items())
        ),
        reveal_position=reveal_pos,
        has_reveal_marker=has_marker,
        ending_shape=_last_sentence_suffix(last_para),
        comparison_patterns=tuple(sorted(set(_extract_words(text, _COMPARISON_WORDS)))),
        misconception_correction_pattern=_misconception_pattern(text),
    )


def _first_sentence_prefix(text: str) -> str:
    sentence = _split_sentences(text)[0] if text else ""
    return sentence[:_OPENING_PREFIX_LEN]


def _last_sentence_suffix(text: str) -> str:
    sentences = _split_sentences(text)
    sentence = sentences[-1] if sentences else ""
    return sentence[-_ENDING_SUFFIX_LEN:] if sentence else ""


_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")


def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text) if s]


def _extract_words(text: str, vocabulary: tuple[str, ...]) -> list[str]:
    return [w for w in vocabulary if w in text]


def _reveal_position(text: str, paragraphs: list[str]) -> tuple[float, bool]:
    """Position of the reveal sentence relative to total sentence count.

    Returns ``(position, found)``. ``0.0`` = reveal at the start,
    ``1.0`` = reveal at the end. ``found=False`` means the draft had no
    reveal marker — the caller must NOT treat that as a similarity
    signal, otherwise two unrelated drafts would both default to 1.0
    and falsely trip the threshold.
    """

    sentences = _split_sentences(text)
    if not sentences:
        return 1.0, False
    reveal_markers = ("原来", "真相是", "事实是", "其实是", "结果是")
    for index, sentence in enumerate(sentences):
        if any(marker in sentence for marker in reveal_markers):
            return index / max(len(sentences) - 1, 1), True
    return 1.0, False


def _misconception_pattern(text: str) -> str:
    """Normalised pattern of how the draft surfaces misconceptions.

    Returns the first matching marker (e.g. ``"很多人以为"``), or an
    empty string when no misconception-correction phrasing is present.
    Empty is the sentinel because :func:`_string_similarity` returns
    ``0.0`` when either side is empty (no signal); a non-empty
    sentinel like ``"none"`` would falsely score ``1.0`` against
    itself.
    """

    for marker in _MISCONCEPTION_WORDS:
        if marker in text:
            return marker
    return ""


# ---------------------------------------------------------------------------
# similarity primitives
# ---------------------------------------------------------------------------


def _string_similarity(a: str, b: str) -> float:
    """Character-set Jaccard similarity. 1.0 = identical character sets.

    Two empty strings return ``0.0`` (no signal) — the alternative
    ``1.0`` would falsely mark "both said nothing" as a similarity
    match and trip :data:`REPETITION_THRESHOLD`.
    """

    if not a or not b:
        return 0.0
    set_a = set(a)
    set_b = set(b)
    return len(set_a & set_b) / len(set_a | set_b)


def _set_similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """Jaccard similarity for two sorted tuples."""

    set_a = set(a)
    set_b = set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _counter_similarity(
    a: tuple[tuple[str, int], ...], b: tuple[tuple[str, int], ...]
) -> float:
    """Jaccard similarity over the keys of two count tuples."""

    keys_a = {key for key, _ in a}
    keys_b = {key for key, _ in b}
    if not keys_a or not keys_b:
        return 0.0
    return len(keys_a & keys_b) / len(keys_a | keys_b)


def _position_similarity(
    a: float, a_found: bool, b: float, b_found: bool
) -> float:
    """Linear similarity in [0, 1]: 1 - |a - b| clamped to >= 0.

    If neither draft had a reveal marker (``a_found`` and ``b_found``
    are both False) the function returns ``0.0`` — the position was
    never declared, so it's not a similarity signal.
    """

    if not a_found or not b_found:
        return 0.0
    return max(0.0, 1.0 - abs(a - b))


# ---------------------------------------------------------------------------
# analysis
# ---------------------------------------------------------------------------


def analyze_repetition(
    draft: DraftRevision, recent_drafts: list[DraftRevision]
) -> RepetitionReport:
    """Compare ``draft`` against ``recent_drafts`` and report similarity per shape.

    Sets ``must_replan=True`` if ANY shape's max similarity across
    ``recent_drafts`` exceeds :data:`REPETITION_THRESHOLD`. The
    contract is "report and replan, do not synonym-swap", so
    ``rewrite_suggestions`` is always ``[]``.

    A draft that contains any banned phrase (e.g. "这就有意思了")
    triggers ``must_replan=True`` even with an empty recent set —
    canned phrasing is an intrinsic property of the draft, not a
    cross-draft comparison.
    """

    text = draft.editorial_text
    banned_hit = any(phrase in text for phrase in _BANNED_PHRASES)

    if banned_hit:
        # Single-draft signal: a banned phrase is an intrinsic property
        # of the draft, not a cross-draft comparison, so we don't have a
        # similarity score. Classify each occurrence by where it lands
        # in the script so the reviewer can see *which* fingerprint
        # surface the banned phrase actually corrupted. If the same
        # phrase appears in multiple zones we surface the first hit;
        # re-runs after a rewrite are encouraged regardless.
        first_index = min(
            (text.index(phrase) for phrase in _BANNED_PHRASES if phrase in text),
            default=0,
        )
        text_len = max(len(text), 1)
        opening_zone = _OPENING_PREFIX_LEN
        ending_zone = max(text_len - _ENDING_SUFFIX_LEN, opening_zone)
        if first_index < opening_zone:
            flagged = "opening_syntax_similarity"
        elif first_index >= ending_zone:
            flagged = "ending_shape_similarity"
        else:
            flagged = "transition_distribution_similarity"
        return RepetitionReport(
            must_replan=True,
            rewrite_suggestions=[],
            **{flagged: 1.0},
        )

    current = compute_fingerprints(draft)
    if not recent_drafts:
        return RepetitionReport(must_replan=False, rewrite_suggestions=[])

    recent_vectors = [compute_fingerprints(other) for other in recent_drafts]

    # Per-shape max similarity across the recent set.
    max_per_shape = [0.0] * 6
    for other in recent_vectors:
        for index, value in enumerate(current.similarity(other)):
            max_per_shape[index] = max(max_per_shape[index], value)

    must_replan = any(value > REPETITION_THRESHOLD for value in max_per_shape)

    return RepetitionReport(
        must_replan=must_replan,
        rewrite_suggestions=[],
        opening_syntax_similarity=max_per_shape[0],
        transition_distribution_similarity=max_per_shape[1],
        reveal_position_similarity=max_per_shape[2],
        ending_shape_similarity=max_per_shape[3],
        comparison_pattern_similarity=max_per_shape[4],
        misconception_correction_pattern_similarity=max_per_shape[5],
    )


__all__ = [
    "REPETITION_THRESHOLD",
    "DraftFingerprints",
    "analyze_repetition",
    "compute_fingerprints",
]