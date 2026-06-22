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
    subs = rv.wrap_to_subcaptions(text, max_chars=20, max_lines=2)
    for sub in subs:
        for line in sub:
            assert_no_midword_break([line], [("Embeddi", "ng"), ("Embed", "ding")])


def test_ffmpeg_protected():
    text = "ffmpeg 是视频处理的瑞士军刀。"
    subs = rv.wrap_to_subcaptions(text, max_chars=20, max_lines=2)
    for sub in subs:
        for line in sub:
            assert_no_midword_break([line], [("ffm", "peg"), ("ff", "mpeg")])


def test_m3u8_protected():
    text = "切片索引写成 m3u8，版本必须是 3。"
    subs = rv.wrap_to_subcaptions(text, max_chars=20, max_lines=2)
    for sub in subs:
        for line in sub:
            assert_no_midword_break([line], [("m3", "u8"), ("m3u", "8")])


def test_h264_protected():
    text = "编码格式是 h264，码率 192k。"
    subs = rv.wrap_to_subcaptions(text, max_chars=20, max_lines=2)
    for sub in subs:
        for line in sub:
            assert_no_midword_break([line], [("h2", "64"), ("h26", "4")])


def test_long_chunk_splits_into_subs():
    # A typical 60-char script chunk should produce >= 2 sub-captions
    text = "video-studio 拆分跑完了。你以为这就叫上线？错。95% 的拆分，死在最后一公里。"
    subs = rv.wrap_to_subcaptions(text, max_chars=20, max_lines=2)
    assert len(subs) >= 2, f"expected >= 2 sub-captions, got {len(subs)}: {subs!r}"
    # Each sub-caption should have <= max_lines lines
    for sub in subs:
        assert len(sub) <= 2, f"sub has too many lines: {sub!r}"
        for line in sub:
            # Each line should be <= max_chars + 2 (tolerance for trailing CJK punct),
            # unless the line ends with an ellipsis (overflow marker).
            visible = line.rstrip("…")
            overflow_ok = (
                len(visible) <= 20
                or (len(visible) <= 22 and visible[-1] in "。！？；，")
                or line.endswith("…")
            )
            assert overflow_ok, f"line exceeds max_chars: {line!r} (len={len(visible)})"


def test_no_orphan_short_lines():
    # A 1-char trailing line like "错" should be merged up if possible.
    text = "video-studio 拆分跑完了。你以为这就叫上线？错"
    subs = rv.wrap_to_subcaptions(text, max_chars=20, max_lines=2)
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
    subs = rv.wrap_to_subcaptions(text, max_chars=20, max_lines=2)
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


def test_decimal_ascii_preserved():
    # "0.5" / "1.5" must never be split into "0." + "5" or "1" + ".5".
    # The tokenize-glue regex keeps them as one token; the packer has no
    # reason to break inside.
    toks = rv._tokenize_for_wrap("前 0.5 秒钩住你")
    assert "0.5" in toks, f"'0.5' got split: {toks!r}"
    # Force a narrow wrap that previously split the number; should now
    # keep it together (and may overflow → ellipsis) rather than split.
    lines = rv._pack_lines("前 0.5 秒钩住你", max_chars=4, max_lines=2)
    joined = "".join(lines)
    assert "0.5" in joined, f"'0.5' got split under narrow wrap: {lines!r}"


def test_decimal_fullwidth_preserved():
    # Full-width period "．" was the actual bug: tokenize didn't include
    # it in the alnum class, so "0．5" became 3 tokens and the packer
    # could orphan "5" on the next line.
    toks = rv._tokenize_for_wrap("前 0．5 秒钩住你")
    assert "0．5" in toks, f"'0．5' got split: {toks!r}"
    lines = rv._pack_lines("前 0．5 秒钩住你", max_chars=4, max_lines=2)
    joined = "".join(lines)
    assert "0．5" in joined, f"'0．5' got split under narrow wrap: {lines!r}"


def test_decimal_with_percent_preserved():
    # "12.5%" is a common pattern; should be one atomic token.
    toks = rv._tokenize_for_wrap("增长 12.5% 后回落")
    assert "12.5%" in toks, f"'12.5%' got split: {toks!r}"
    # max_chars=8 fits "增长 12.5%" (7 chars) on one line; verify the
    # number+percent stays glued across the line boundary, not split
    # between "12." and "5%".
    lines = rv._pack_lines("增长 12.5% 后回落", max_chars=8, max_lines=2)
    joined = "".join(lines)
    assert "12.5%" in joined, f"'12.5%' got split under wrap: {lines!r}"
    # And no line should start with "5%" or "5 %" (the orphan pattern)
    for ln in lines:
        assert not ln.lstrip().startswith("5"), f"orphan '5%' on new line: {ln!r}"


# ────────────────────────────────────────────────────────────────────

def test_v3_19char_single_line():
    # User reported: 19-char subtitle '一套150万的房子 三十年等额本息还完'
    # was being split to 2 lines (9/9) under max_chars=18. Bumping to
    # max_chars=20 lets it stay on a single line.
    text = "一套150万的房子 三十年等额本息还完"
    assert len(text) == 19, f"setup error: text is {len(text)} chars, expected 19"
    lines = rv.wrap_caption_lines(text, max_chars=20, max_lines=2)
    assert lines == [text], f"expected single line at max=20, got {lines!r}"


def test_v3_20char_single_line():
    # Boundary: exactly 20 chars should fit on one line.
    text = "是一套150万的房子 三十年等额本息还完"  # 20 chars
    assert len(text) == 20, f"setup error: text is {len(text)} chars, expected 20"
    lines = rv.wrap_caption_lines(text, max_chars=20, max_lines=2)
    assert lines == [text], f"expected single line at max=20, got {lines!r}"


def test_v3_21char_balanced_split():
    # 21 chars exceeds 20, should split near midpoint.
    text = "是一套150万的房子 三十年等额本息还完X"  # 21 chars
    assert len(text) == 21, f"setup error: text is {len(text)} chars, expected 21"
    lines = rv.wrap_caption_lines(text, max_chars=20, max_lines=2)
    assert len(lines) == 2, f"expected 2 lines, got {lines!r}"
    for ln in lines:
        assert len(ln) <= 20, f"line exceeds max_chars: {ln!r} (len={len(ln)})"
    # All non-space chars must be preserved (spaces may be eaten at split boundary)
    joined = "".join(lines)
    assert joined.replace(" ", "") == text.replace(" ", ""), (
        f"non-space chars lost in wrap: {text!r} -> {lines!r}"
    )
    # Both halves should be close to balanced (10/11 or 11/10)
    sizes = [len(ln) for ln in lines]
    assert abs(sizes[0] - sizes[1]) <= 2, f"unbalanced split: {sizes} from {text!r}"


def test_v3_18char_single_line():
    # 18-char subtitle stays single-line at max=20.
    text = "是你以为自己在还钱 银行在给你算时间"
    lines = rv.wrap_caption_lines(text, max_chars=20, max_lines=2)
    assert lines == [text], f"expected single line, got {lines!r}"


def test_v9_strict_punct_split():
    # v9: every _SPLIT_PUNCT (including 、 and ASCII ,) becomes a sub
    # boundary. Trigger sentence "一个能秒掉整个朝代的神仙,忍了,这
    # 一忍就是整整28年,中间隔了2次封神、3次朝堂清洗、5次人间王朝更
    # 替,你就知道这克制有多深。" should split into 7 subs matching
    # the user's expected layout:
    #   一个能秒掉整个朝代的神仙 / 忍了 / 这一忍就是整整28年 /
    #   中间隔了2次封神 / 3次朝堂清洗 / 5次人间王朝更替 /
    #   你就知道这克制有多深
    text = "一个能秒掉整个朝代的神仙,忍了,这一忍就是整整28年,中间隔了2次封神、3次朝堂清洗、5次人间王朝更替,你就知道这克制有多深。"
    subs = rv._split_sentence_into_subs(text, max_chars=20, hard_max=20)
    expected = [
        "一个能秒掉整个朝代的神仙",
        "忍了",
        "这一忍就是整整28年",
        "中间隔了2次封神",
        "3次朝堂清洗",
        "5次人间王朝更替",
        "你就知道这克制有多深",
    ]
    actual = [rv._strip_punctuation(s).strip() for s in subs]
    assert actual == expected, f"got {actual!r}, expected {expected!r}"


def test_v9_single_clause_no_punct_stays_whole():
    # v9: a 26-char sentence with no _SPLIT_PUNCT boundary (`/` and decimal
    # `.` are NOT in `_SPLIT_PUNCT`) must stay whole as one sub, even though
    # it exceeds max_chars=20. The user reported this exact sentence being
    # fragmented mid-clause at `的` particle, breaking readability.
    # Downstream wrap_caption_lines handles the multi-line display.
    text = "7 年内的死亡率比继续参与工作的/组高出 2.3 倍"
    assert len(text) > 20, f"setup: text must exceed max_chars=20, got {len(text)}"
    subs = rv._split_sentence_into_subs(text, max_chars=20, hard_max=30)
    assert len(subs) == 1, (
        f"single-clause sentence must stay whole, got {len(subs)} subs: {subs!r}"
    )
    assert subs[0] == text, f"sub content drifted: {subs[0]!r} vs {text!r}"
    print("✓ single-clause sentence (no _SPLIT_PUNCT) stays whole")


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
        test_decimal_ascii_preserved,
        test_decimal_fullwidth_preserved,
        test_decimal_with_percent_preserved,
        test_v3_19char_single_line,
        test_v3_20char_single_line,
        test_v3_21char_balanced_split,
        test_v3_18char_single_line,
        test_v9_strict_punct_split,
        test_v9_single_clause_no_punct_stays_whole,
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
