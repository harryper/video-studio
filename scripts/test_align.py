#!/usr/bin/env python3
"""Unit tests for the aligner sentence post-process.

Run: python3 scripts/test_align.py

Background
----------
The aligner (`align_audio_stable_ts.py`) splits sentences on `。！？!?.`,
which puts ASCII period in the split set. That correctly terminates
English sentences ("i.e. 5" → "i.e." + "5") but also severs decimal
numbers in the middle: "前 0.5 秒钩住你" becomes "前 0." + "5 秒钩住你".
Downstream consumers (preview_caption_ffmpeg) use sentences[].text
verbatim as subtitle text, so a mid-decimal split shows up as
"前0." on one line and "5秒钩住你" on the next in the rendered mp4.

`_merge_decimal_split_sentences` re-glues pairs that are obviously
two halves of the same decimal number. These tests pin down the
behavior so the heuristic doesn't drift.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import align_audio_stable_ts as al  # noqa: E402


def _s(text, start=0.0, end=0.0, indices=None):
    return {
        "text": text,
        "start": start,
        "end": end,
        "word_indices": indices if indices is not None else list(range(len(text))),
    }


def test_decimal_end_split_merged():
    """The real-world v_5dec0abc case: 3 fragments → 1 sentence."""
    out = al._merge_decimal_split_sentences([
        _s("这不是玄学，是节奏——前0.", 4.76, 7.42, list(range(14))),
        _s("5秒钩住你，中间每1.", 7.50, 8.94, list(range(14, 25))),
        _s("5秒一个新刺激。", 9.00, 10.04, list(range(25, 33))),
    ])
    assert len(out) == 1, f"expected 1 merged sentence, got {len(out)}: {[s['text'] for s in out]}"
    merged = out[0]
    assert "0.5秒" in merged["text"], f"0.5 not re-glued: {merged['text']!r}"
    assert "1.5秒" in merged["text"], f"1.5 not re-glued: {merged['text']!r}"
    # Span should be the union of all three
    assert merged["start"] == 4.76
    assert merged["end"] == 10.04
    # word_indices should be the concatenation
    assert merged["word_indices"] == list(range(33))


def test_decimal_with_percent_merged():
    """12.5% pattern: '12.' + '5% 增长' should glue to '12.5% 增长'."""
    out = al._merge_decimal_split_sentences([
        _s("增长 12.", 1.0, 1.4, [0, 1, 2, 3, 4]),
        _s("5% 后回落。", 1.4, 2.0, [5, 6, 7, 8]),
    ])
    assert len(out) == 1, f"expected 1, got {len(out)}: {[s['text'] for s in out]}"
    assert out[0]["text"] == "增长 12.5% 后回落。"
    assert out[0]["start"] == 1.0
    assert out[0]["end"] == 2.0


def test_i_e_5_not_merged():
    """English abbreviation 'i.e.' followed by '5 percent' must NOT merge:
    the period is preceded by 'e' (a letter), not a digit, so the
    decimal-point heuristic correctly rejects it."""
    out = al._merge_decimal_split_sentences([
        _s("i.e.", 1.0, 1.2, [0, 1, 2, 3]),
        _s("5 percent", 1.3, 1.8, [4, 5]),
    ])
    assert len(out) == 2, f"i.e. / 5 should NOT merge, got {len(out)}: {[s['text'] for s in out]}"
    assert out[0]["text"] == "i.e."
    assert out[1]["text"] == "5 percent"


def test_three_way_merge():
    """'1.5.5' (chained decimals): ['1.', '5.', '5'] must collapse in one
    pass via the re-scan-from-same-index loop, ending as ['1.5.5']."""
    out = al._merge_decimal_split_sentences([
        _s("1.", 0.0, 0.1, [0]),
        _s("5.", 0.1, 0.2, [1]),
        _s("5", 0.2, 0.3, [2]),
    ])
    assert len(out) == 1, f"expected 1, got {len(out)}: {[s['text'] for s in out]}"
    assert out[0]["text"] == "1.5.5", f"expected '1.5.5', got {out[0]['text']!r}"


def test_no_split_no_change():
    """Adjacent normal sentences with letter endings/startings are
    passed through untouched."""
    src = [
        _s("第一句。", 0.0, 1.0, [0, 1, 2, 3]),
        _s("第二句。", 1.0, 2.0, [4, 5, 6, 7]),
    ]
    out = al._merge_decimal_split_sentences(src)
    assert len(out) == 2
    assert out[0]["text"] == "第一句。"
    assert out[1]["text"] == "第二句。"
    # Input list must not be mutated
    assert src[0]["text"] == "第一句。"


def test_empty_input():
    """Defensive: empty list should return empty list, not crash."""
    assert al._merge_decimal_split_sentences([]) == []


def test_single_sentence():
    """Single-sentence input is a no-op."""
    out = al._merge_decimal_split_sentences([_s("只一句。", 0.0, 1.0, [0, 1, 2])])
    assert len(out) == 1
    assert out[0]["text"] == "只一句。"


def test_period_preceded_by_letter_not_merged():
    """'Dr.' + 'Smith' should stay split — the period is preceded by 'r'."""
    out = al._merge_decimal_split_sentences([
        _s("Dr.", 0.0, 0.2, [0, 1]),
        _s("Smith said hi.", 0.2, 1.0, [3, 4, 5, 6, 7, 8]),
    ])
    assert len(out) == 2, f"Dr. / Smith should NOT merge: {[s['text'] for s in out]}"


def test_chinese_full_stop_still_splits():
    """Chinese full stop `。` is still a hard split — the post-process
    only re-glues decimal-period splits, not real sentence boundaries."""
    src = [
        _s("前0.5秒钩住你。", 0.0, 1.0, [0, 1, 2, 3, 4, 5, 6, 7]),
        _s("中间每1.5秒。", 1.0, 2.0, [8, 9, 10, 11, 12, 13, 14]),
    ]
    out = al._merge_decimal_split_sentences(src)
    # `。` is not the same as `.` so neither fragment ends with `.`,
    # thus no merge should happen
    assert len(out) == 2, f"Chinese full-stop should keep sentences split: {[s['text'] for s in out]}"


# ────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_decimal_end_split_merged,
        test_decimal_with_percent_merged,
        test_i_e_5_not_merged,
        test_three_way_merge,
        test_no_split_no_change,
        test_empty_input,
        test_single_sentence,
        test_period_preceded_by_letter_not_merged,
        test_chinese_full_stop_still_splits,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
