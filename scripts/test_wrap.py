#!/usr/bin/env python3
"""Unit tests for the word-aware caption wrap and sub-caption splitter.

Run: python3 scripts/test_wrap.py
"""
import sys
from pathlib import Path

# Import the daemon module to get the wrap functions. This works because
# the module's only side effects at import time are stdlib imports + constant
# assignments; the daemon logic is all inside main() / process_one().
sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as rv  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Test cases: (input_text, max_chars, max_lines, expected_substr_in_each_line)
# Each test asserts:
#   1) no line ends mid-English-word (e.g. "Embeddi" + "ng")
#   2) no line exceeds max_chars
#   3) at most max_lines lines per sub-caption
# ────────────────────────────────────────────────────────────────────

def assert_no_midword_break(lines, forbidden_pairs):
    """Ensure no line is the second half of an English word."""
    for line in lines:
        for prev, cont in forbidden_pairs:
            if cont and (line == cont or line.startswith(cont + " ") or line.startswith(cont)):
                raise AssertionError(
                    f"mid-word break detected: line starts with {cont!r} "
                    f"(expected {prev!r}+{cont!r} to stay together) -> {line!r}"
                )


def test_basic_chinese_short():
    lines = rv.wrap_caption_lines("你好世界", max_chars=10, max_lines=2)
    assert lines == ["你好世界"], f"expected no wrap, got {lines!r}"


def test_chinese_with_english_word_protected():
    # "Embedding" must not be split into "Embeddi" + "ng"
    text = "第一个，Embedding。每个编号翻译成坐标。"
    subs = rv.wrap_to_subcaptions(text, max_chars=18, max_lines=2)
    for sub in subs:
        for line in sub:
            assert_no_midword_break([line], [("Embeddi", "ng"), ("Embed", "ding")])


def test_ffmpeg_protected():
    text = "ffmpeg 是视频处理的瑞士军刀。"
    subs = rv.wrap_to_subcaptions(text, max_chars=18, max_lines=2)
    for sub in subs:
        for line in sub:
            assert_no_midword_break([line], [("ffm", "peg"), ("ff", "mpeg")])


def test_m3u8_protected():
    text = "切片索引写成 m3u8，版本必须是 3。"
    subs = rv.wrap_to_subcaptions(text, max_chars=18, max_lines=2)
    for sub in subs:
        for line in sub:
            assert_no_midword_break([line], [("m3", "u8"), ("m3u", "8")])


def test_h264_protected():
    text = "编码格式是 h264，码率 192k。"
    subs = rv.wrap_to_subcaptions(text, max_chars=18, max_lines=2)
    for sub in subs:
        for line in sub:
            assert_no_midword_break([line], [("h2", "64"), ("h26", "4")])


def test_long_chunk_splits_into_subs():
    # A typical 60-char script chunk should produce >= 2 sub-captions
    text = "video-studio 拆分跑完了。你以为这就叫上线？错。95% 的拆分，死在最后一公里。"
    subs = rv.wrap_to_subcaptions(text, max_chars=18, max_lines=2)
    assert len(subs) >= 2, f"expected >= 2 sub-captions, got {len(subs)}: {subs!r}"
    # Each sub-caption should have <= max_lines lines
    for sub in subs:
        assert len(sub) <= 2, f"sub has too many lines: {sub!r}"
        for line in sub:
            # Each line should be <= max_chars + 2 (tolerance for trailing CJK punct),
            # unless the line ends with an ellipsis (overflow marker).
            visible = line.rstrip("…")
            overflow_ok = (
                len(visible) <= 18
                or (len(visible) <= 20 and visible[-1] in "。！？；，")
                or line.endswith("…")
            )
            assert overflow_ok, f"line exceeds max_chars: {line!r} (len={len(visible)})"


def test_no_orphan_short_lines():
    # A 1-char trailing line like "错" should be merged up if possible.
    text = "video-studio 拆分跑完了。你以为这就叫上线？错"
    subs = rv.wrap_to_subcaptions(text, max_chars=18, max_lines=2)
    for sub in subs:
        for line in sub:
            if len(line) < 4:
                # Any 1-2 char orphan is bad
                raise AssertionError(
                    f"orphan short line {line!r} in sub {sub!r}"
                )


def test_tokenize_does_not_split_english():
    toks = rv._tokenize_for_wrap("Embedding 是 Embedding 的中文")
    # "Embedding" should be a single token
    assert "Embedding" in toks, f"Embedding was split into {toks!r}"
    # "的" should be a single token (single CJK)
    assert "的" in toks


def test_ellipsis_on_overflow():
    # Force overflow: 60 chars into max_chars=10, max_lines=2 = 20 chars budget
    text = "abcdefghij" * 6  # 60 chars
    subs = rv.wrap_to_subcaptions(text, max_chars=10, max_lines=2)
    # The very last sub-caption should end with … (or its last line should)
    last_sub = subs[-1]
    last_line = last_sub[-1]
    assert last_line.endswith("…"), f"expected trailing …, got {last_sub!r}"


def test_realistic_script_chunk():
    # Use a real-shaped chunk from the existing v_35ea329c run
    text = (
        "video-studio 拆分跑完了。你以为这就叫上线？错。"
        "95% 的拆分，死在最后一公里。"
        "前面 14 天搭骨架、调接口、过联调，全过。"
    )
    subs = rv.wrap_to_subcaptions(text, max_chars=18, max_lines=2)
    print(f"\n  chunk ({len(text)} chars) -> {len(subs)} sub-caption(s):")
    for j, sub in enumerate(subs):
        print(f"    sub-{j+1}: {sub}")
    # No mid-word breaks across the full output
    flat = [line for sub in subs for line in sub]
    assert_no_midword_break(flat, [("Embeddi", "ng"), ("ffm", "peg"), ("m3", "u8")])
    # Numbers must be preserved (digit tokens were silently dropped in v1)
    joined = "".join(flat)
    assert "95" in joined, f"'95' lost in wrapping: {flat!r}"
    assert "14" in joined, f"'14' lost in wrapping: {flat!r}"


def test_number_and_percent_preserved():
    text = "95% 的拆分，前面 14 天搭骨架。"
    subs = rv.wrap_to_subcaptions(text, max_chars=14, max_lines=2)
    flat = [line for sub in subs for line in sub]
    joined = "".join(flat)
    assert "95%" in joined, f"'95%' got mangled: {flat!r}"
    assert "14" in joined, f"'14' got mangled: {flat!r}"


# ────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_basic_chinese_short,
        test_chinese_with_english_word_protected,
        test_ffmpeg_protected,
        test_m3u8_protected,
        test_h264_protected,
        test_long_chunk_splits_into_subs,
        test_no_orphan_short_lines,
        test_tokenize_does_not_split_english,
        test_ellipsis_on_overflow,
        test_number_and_percent_preserved,
        test_realistic_script_chunk,
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
