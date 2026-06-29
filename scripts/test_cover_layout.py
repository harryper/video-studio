#!/usr/bin/env python3
"""Tests for cover layout (script daemon parse_cover_validation + render daemon
cover_fallback + render_cover_layout). 5 cases per the design plan.

Run: cd /root/.openclaw/workspace/skills/video-studio/scripts && python3 test_cover_layout.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as rv  # noqa: E402
import process_video_script_jobs as sv  # noqa: E402
import process_video_narrate_jobs as nv  # noqa: E402


def test_render_cover_layout_basic():
    """Standard cover dict → main + highlight + sub all visible in HTML."""
    cover = {
        "main": "糖不是调味品",
        "main_highlight": [2, 4],
        "sub": "二战真相比你想的更狠",
    }
    html = rv.render_cover_layout(cover, scene_idx=1)
    # main is split into pre ("糖不") / hl ("是调") / post ("味品"). The full
    # original main text still appears (concatenated), just split across spans.
    assert "糖" in html and "不" in html
    assert "是调" in html  # the yellow highlight slice
    assert "味品" in html  # post-highlight tail
    assert "cover-hl" in html
    assert "二战真相比你想的更狠" in html
    # Landscape orientation is the default (1920x1080)
    assert "cover-landscape" in html


def test_render_cover_layout_highlight_oob_safe():
    """OOB highlight index must clamp, not crash Puppeteer."""
    cover = {"main": "短的", "main_highlight": [2, 4], "sub": ""}
    # main is 2 chars, hl is [2,4] which is OOB. Must clamp, not throw.
    html = rv.render_cover_layout(cover, scene_idx=1)
    assert html
    # After clamp, hl slice is empty string — page should still render
    assert "短的" in html


def test_cover_fallback_with_hook_word():
    """Script with a hook word (但是/其实/真相是/...) → main picks that sentence."""
    script = "但是糖从来不是调味品,真相让你吃惊。然后还有更多八卦。"
    fb = rv.cover_fallback(script)
    assert 1 <= len(fb["main"]) <= 8, f"main too long: {fb['main']!r}"
    assert len(fb["main_highlight"]) == 2
    assert fb["main_highlight"][0] < fb["main_highlight"][1]
    assert fb["main_highlight"][1] <= len(fb["main"])
    # Multi-sentence script should produce a sub — either the next hook
    # sentence or any sentence that isn't main.
    assert fb["sub"], f"sub should be non-empty for multi-sentence script, got {fb!r}"


def test_cover_fallback_empty_script():
    """Empty script → no crash, returns a dict with empty main for graceful fallback."""
    fb = rv.cover_fallback("")
    assert isinstance(fb, dict)
    assert "main" in fb
    # main can be empty for empty script (render path checks before injecting)


def test_parse_cover_validates_index_bounds():
    """parse_cover_validation rejects OOB and reversed indices, accepts valid."""
    # OOB end → None
    assert sv.parse_cover_validation({"main": "短的", "main_highlight": [0, 5]}) is None
    # OOB start (negative) → None
    assert sv.parse_cover_validation({"main": "短的", "main_highlight": [-1, 2]}) is None
    # Reversed indices → None
    assert sv.parse_cover_validation({"main": "短的", "main_highlight": [3, 1]}) is None
    # Equal start/end → None (empty highlight is useless)
    assert sv.parse_cover_validation({"main": "短的", "main_highlight": [1, 1]}) is None
    # Valid → returns dict
    ok = sv.parse_cover_validation({"main": "糖的真相", "main_highlight": [1, 3]})
    assert ok == {"main": "糖的真相", "main_highlight": [1, 3], "sub": ""}
    # Empty main → None
    assert sv.parse_cover_validation({"main": "", "main_highlight": [0, 0]}) is None
    # Too long main → None
    assert sv.parse_cover_validation({"main": "九个字符以上的main", "main_highlight": [0, 3]}) is None


def test_cover_portrait_orientation():
    """9:16 (portrait) width/height picks cover-portrait CSS, larger font."""
    cover = {
        "main": "糖不是调味品",
        "main_highlight": [2, 4],
        "sub": "二战真相比你想的更狠",
    }
    html = rv.render_cover_layout(cover, scene_idx=1, width=1080, height=1920)
    assert "cover-portrait" in html
    assert "cover-landscape" not in html


def test_cover_landscape_orientation():
    """16:9 (landscape) — explicit width/height picks landscape CSS."""
    cover = {"main": "test", "main_highlight": [0, 2], "sub": "sub"}
    html = rv.render_cover_layout(cover, scene_idx=1, width=1920, height=1080)
    assert "cover-landscape" in html
    assert "cover-portrait" not in html


def test_cover_prompt_requires_hook_not_question():
    """v3: prompt must require main to be a hook (反常识/颠覆), forbid question marks."""
    instr = sv.COVER_INSTRUCTIONS
    assert any(kw in instr for kw in ["钩子", "反常识", "颠覆", "数字冲击"]), \
        "COVER_INSTRUCTIONS must require main to be a hook"
    assert any(anti in instr for anti in ["问号", "不准.*问", "？"]), \
        "COVER_INSTRUCTIONS must forbid question-mark endings"
    assert "6 字" in instr or "6字" in instr, "main 6-char limit must remain"


def test_cover_prompt_sub_no_spoiler():
    """v3: prompt must forbid sub from spoiling main's answer."""
    instr = sv.COVER_INSTRUCTIONS
    assert any(kw in instr for kw in ["严禁剧透", "不准剧透", "剧透"]), \
        "COVER_INSTRUCTIONS must forbid sub spoiling main"
    assert "因为" in instr and "所以" in instr, \
        "prompt must explicitly forbid '因为...所以...' spoiler pattern"
    assert "真相是" in instr and "直接说" in instr, \
        "prompt must forbid '真相是' / '直接说答案' teases"


def test_cover_fallback_finds_hook_sentence():
    """v3: fallback finds the 反常识词 sentence, not mechanical chunk[0][:6].

    Script uses period-separated sentences so the hook sentence ('其实...')
    is its own sentence, not lumped into the first.
    """
    script = "糖在二战被列为战略物资。其实糖从来不是调味品,而是热量来源。"
    fb = rv.cover_fallback(script)
    main = fb["main"]
    assert not main.startswith("糖在二战"), \
        f"fallback must not mechanically truncate first sentence, got {main!r}"
    assert 1 <= len(main) <= 8, f"main length out of range: {main!r}"
    assert fb["main_highlight"][0] > 0, \
        f"highlight must not fall on first char (v3), got hl={fb['main_highlight']!r}"


def test_cover_validate_rejects_first_char_highlight():
    """v3: hl[0] == 0 → None (first char not allowed as highlight)."""
    assert sv.parse_cover_validation(
        {"main": "糖不是调味品", "main_highlight": [0, 2], "sub": ""}
    ) is None


def test_cover_validate_rejects_last_char_highlight():
    """v3: hl[1] >= len(main) → None (last char not allowed)."""
    assert sv.parse_cover_validation(
        {"main": "糖不是调味品", "main_highlight": [5, 6], "sub": ""}
    ) is None


def test_cover_validate_rejects_full_span_highlight():
    """v3: hl range > 3 → None (no full-span highlight)."""
    assert sv.parse_cover_validation(
        {"main": "糖不是调味品", "main_highlight": [1, 5], "sub": ""}
    ) is None


def test_cover_validate_rejects_question_mark_main():
    """v3: main ending with ?/。/! → None (hook must not be a question)."""
    assert sv.parse_cover_validation(
        {"main": "糖为什么被列？", "main_highlight": [1, 3], "sub": ""}
    ) is None


def test_cover_validate_rejects_sub_spoiler_because():
    """v3: sub containing '因为.../直接说...' → None (spoils main's answer)."""
    assert sv.parse_cover_validation({
        "main": "糖不是调味品", "main_highlight": [2, 4],
        "sub": "直接说答案,因为糖的本质不是调味品"
    }) is None


def test_cover_validate_rejects_sub_spoiler_truth():
    """v3: sub containing '真相是' → None."""
    assert sv.parse_cover_validation({
        "main": "糖不是调味品", "main_highlight": [2, 4],
        "sub": "真相是,它是热量来源"
    }) is None


def test_cover_subtimes_index_and_time_shift():
    """Bug 3 fix: when cover is present, content scene i must use subtimes[i-1]
    AND slot_start/end must be shifted by COVER_DURATION_SEC so TTS time
    maps to video time.
    """
    import re as _re
    fake_subtimes = [
        [("chunks0 subs", 0.0, 5.75)],   # chunks[0]: TTS [0, 5.75]
        [("chunks1 subs", 5.75, 11.5)],  # chunks[1]: TTS [5.75, 11.5]
        [("chunks2 subs", 11.5, 17.25)], # chunks[2]: TTS [11.5, 17.25]
    ]
    fake_chunks = ["chunks0 text", "chunks1 text", "chunks2 text"]
    fake_media = [("image", Path("/tmp/scene_1.jpg"))] * 3
    cover = {
        "main": "test",
        "main_highlight": [0, 2],
        "sub": "sub",
        "_image_path": "/tmp/scene_0.jpg",
    }
    html = rv.build_image_composition_html(
        fake_media, fake_chunks, total_duration=20.0,
        width=1920, height=1080, cover=cover, subtimes=fake_subtimes,
    )
    # Content scene 1 (HTML scene-2) should use chunks[0]'s subs.
    # Each char is rendered as its own <div class="cap-line">x</div>, so we
    # split at the next scene and pull the cap-line sequence inside sub-2-1.
    parts = html.split('id="sub-2-1">', 1)
    assert len(parts) == 2, "sub-2-1 not found in HTML"
    after = parts[1]
    # Take the substring up to the next <div id="scene-
    end = after.find('<div id="scene-')
    if end == -1:
        end = len(after)
    block = after[:end]
    cap_lines = _re.findall(r'class="cap-line">([^<]+)</div>', block)
    text = "".join(cap_lines)
    # Bug 3a fix: content scene 1 must show chunks[0]'s subs, not chunks[1]'s
    assert text == "chunks0 subs", \
        f"sub-2-1 text should be 'chunks0 subs' (chunks[0] via shifted subtimes), got {text!r}"
    # sub-2-1 must not contain chunks[1]'s text (the off-by-one bug)
    assert "chunks1 subs" not in html, \
        "chunks[1]'s subs must NOT appear in scene-2 (was bug 3a)"
    # Bug 3b fix: tween timing — sub-2-1 fade-in should be at
    # TTS[0]+COVER = 0+2.5=2.5, NOT 5.75
    m = _re.search(r"#sub-2-1.*?opacity:\s*1.*?duration:\s*0\.2.*?,\s*([\d.]+)\)", html)
    assert m, "couldn't find sub-2-1 fade-in tween"
    fade_in = float(m.group(1))
    assert abs(fade_in - rv.COVER_DURATION_SEC) < 0.01, \
        f"sub-2-1 fade-in should be at COVER_DURATION_SEC={rv.COVER_DURATION_SEC}, got {fade_in}"


def test_cover_validate_rejects_half_word_highlight():
    """v3.1: hl "是调" on "糖不是调味品" [2,4] → None (not a complete word).

    是=copula, 调=start of 调味品 (truncated). Together "是调" carries
    no hook value — neither is in the hook-words whitelist. LLM picking
    this gets reject'd so the fallback can take over with a real hook.
    """
    assert sv.parse_cover_validation(
        {"main": "糖不是调味品", "main_highlight": [2, 4], "sub": ""}
    ) is None


def test_cover_validate_accepts_not_highlight():
    """v3.1: hl "不是" on "糖不是调味品" [1,3] → valid (full negation word)."""
    ok = sv.parse_cover_validation(
        {"main": "糖不是调味品", "main_highlight": [1, 3], "sub": ""}
    )
    assert ok == {"main": "糖不是调味品", "main_highlight": [1, 3], "sub": ""}


def test_cover_validate_rejects_prep_in_highlight():
    """v3.1: hl "被列" on "糖在二战被列为" [5,7] → None (no hook).

    Variant of the "是调" bug: LLM picking two consecutive non-hook
    structural chars. 被/列 are passive-particle/verb, neither in the
    hook-words whitelist. Catches the pattern even when the main string
    differs from the original repro.
    """
    assert sv.parse_cover_validation(
        {"main": "糖在二战被列为", "main_highlight": [5, 7], "sub": ""}
    ) is None


def test_cover_validate_accepts_number_highlight():
    """v3.1: hl with a digit is always accepted (numeric hook).

    "一成" on "白糖只占一成热量" — "一" is a digit-like hook char.
    """
    ok = sv.parse_cover_validation(
        {"main": "白糖占一成热量", "main_highlight": [3, 5], "sub": ""}
    )
    assert ok is not None, "一成 should be accepted (digit hook)"


def test_cover_present_first_scene_starts_at_cover_end():
    """Bug 2 fix: when cover is present, the first content scene's
    data-start must equal COVER_DURATION_SEC (not TTS[0]+COVER), so the
    image starts the moment cover ends — no 0.3s black gap while TTS
    is already speaking.

    Construct: TTS[0] of chunks[0] = 0.3 (TTS synthesis initial silence).
    Without the fix: scene-2 data-start = 0.3 + 2.5 = 2.8 (black 2.5-2.8s).
    With the fix: scene-2 data-start = 2.5 (cover end).
    """
    import re as _re
    fake_subtimes = [
        [("chunks0 subs", 0.3, 5.75)],  # TTS[0] = 0.3 (not 0) — the bug repro
        [("chunks1 subs", 5.75, 11.5)],
    ]
    fake_chunks = ["chunks0 text", "chunks1 text"]
    fake_media = [("image", Path("/tmp/scene_1.jpg"))] * 2
    cover = {
        "main": "test",
        "main_highlight": [0, 2],
        "sub": "sub",
        "_image_path": "/tmp/scene_0.jpg",
    }
    html = rv.build_image_composition_html(
        fake_media, fake_chunks, total_duration=12.0,
        width=1920, height=1080, cover=cover, subtimes=fake_subtimes,
    )
    # Find scene-2 (first content scene) data-start
    m = _re.search(r'<div id="scene-2"[^>]*data-start="([\d.]+)"', html)
    assert m, "scene-2 not found in HTML"
    scene_start = float(m.group(1))
    assert scene_start == rv.COVER_DURATION_SEC, (
        f"scene-2 data-start should be COVER_DURATION_SEC={rv.COVER_DURATION_SEC} "
        f"(no black gap), got {scene_start}"
    )


def test_cover_fallback_picks_hook_highlight():
    """v3.1: fallback should pick a hook word as highlight when possible.

    Script: "但其实糖从来不是调味品" — hook markers 但是/其实 present, so
    main = "但其实糖从来" (6 chars). The candidate [1,3]="其实" contains
    the hook word "其实" — fallback should pick that, not the default
    [1,3] which is "其实" here anyway, or fall back to "实" [1,2].
    """
    script = "但其实糖从来不是调味品,真相让你吃惊。"
    fb = rv.cover_fallback(script)
    L = len(fb["main"])
    s, e = fb["main_highlight"]
    # Should be a valid hook slice (not just "是糖" or other common chars)
    if 0 < s < e <= L:
        slice_ = fb["main"][s:e]
        assert sv._is_valid_highlight(slice_), \
            f"fallback highlight {slice_!r} on {fb['main']!r} is not a hook word"


def test_cover_audio_padded_when_delay_set():
    """v3.2: merge_video_audio with audio_delay_sec=0.8 must build a filter
    chain that prepends adelay=800|800 so the audio starts at video time
    0.8s (cover end), and the output duration covers both streams.

    Without this, the first audible word plays during the cover scene
    (audio 0.32s in, cover ends at 0.8s) — user hears 0.48s of TTS ahead
    of any visual cue. With adelay, audio waits until 0.8s, syncing
    with sub-2-1 fade-in (TTS[0] + COVER = 0.32 + 0.8 = 1.12s, but the
    silent gap from 0.8-1.12 is intentional, matching TTS initial silence).
    """
    import unittest.mock as _mock

    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Return a successful CompletedProcess so merge doesn't raise
        return _mock.Mock(returncode=0, stderr="", stdout="")

    with _mock.patch.object(nv, "get_duration_sec", side_effect=lambda p: 55.5), \
         _mock.patch.object(nv.subprocess, "run", side_effect=fake_run):
        nv.merge_video_audio(
            Path("/tmp/fake.mp4"), Path("/tmp/fake.mp3"),
            Path("/tmp/out.mp4"), audio_delay_sec=0.8,
        )

    cmd = captured["cmd"]
    # ffmpeg -filter_complex is the arg after "-filter_complex"
    fc_idx = cmd.index("-filter_complex") + 1
    fc = cmd[fc_idx]
    assert "adelay=800|800" in fc, \
        f"filter_complex must contain adelay=800|800, got: {fc}"
    # -t output_duration must cover audio+delay
    t_idx = cmd.index("-t") + 1
    assert float(cmd[t_idx]) >= 56.3, \
        f"output duration must cover audio(55.5)+delay(0.8)=56.3, got {cmd[t_idx]}"


def test_cover_no_audio_delay_when_no_cover():
    """v3.2: merge_video_audio without delay uses the legacy anull filter.

    When cover.json is absent, audio_delay_sec=0.0 must produce the
    pre-v3.2 filter (no adelay) so existing jobs without covers aren't
    affected.
    """
    import unittest.mock as _mock

    captured = {}
    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _mock.Mock(returncode=0, stderr="", stdout="")

    with _mock.patch.object(nv, "get_duration_sec", side_effect=lambda p: 55.5), \
         _mock.patch.object(nv.subprocess, "run", side_effect=fake_run):
        nv.merge_video_audio(
            Path("/tmp/fake.mp4"), Path("/tmp/fake.mp3"),
            Path("/tmp/out.mp4"),
        )

    cmd = captured["cmd"]
    fc_idx = cmd.index("-filter_complex") + 1
    fc = cmd[fc_idx]
    assert "adelay" not in fc, \
        f"no cover → no adelay expected, got: {fc}"
    assert "anull" in fc, f"no cover → anull passthrough expected, got: {fc}"


def test_cover_sub_single_line_nowrap():
    """v3.3: .cover-sub must have white-space: nowrap to keep sub single-line.

    Without this, sub text exceeding max-width:80% wraps to 2 lines in
    portrait (1080x1920, 80% = 864px) or when CJK fonts render wider than
    1em on real Chrome. .cover-main already has nowrap — the asymmetry
    was the bug.
    """
    # Read the full HTML to get the embedded <style> block. render_cover_layout
    # only returns the cover <div>, so we go through build_image_composition_html
    # (with a real sub) to pull the style.
    cover = {
        "main": "糖不是调味品",
        "main_highlight": [1, 3],
        "sub": "一吨西瓜榨出的糖不到甘蔗的三分之一",
    }
    fake_subtimes = [
        [("c0", 0.0, 5.0)],
        [("c1", 5.0, 10.0)],
    ]
    fake_chunks = ["c0 text", "c1 text"]
    fake_media = [("image", Path("/tmp/scene_1.jpg"))] * 2
    cover_with_img = dict(cover, _image_path="/tmp/scene_0.jpg")
    html = rv.build_image_composition_html(
        fake_media, fake_chunks, total_duration=10.0,
        width=1920, height=1080, cover=cover_with_img, subtimes=fake_subtimes,
    )
    # The CSS is inside a f-string so {{ and }} are escaped in the source but
    # the rendered HTML has single { and }. Assert against the rendered form.
    assert ".cover-sub" in html, "no .cover-sub rule in HTML"
    # Find the .cover-sub block and check it has nowrap
    import re as _re
    m = _re.search(r"\.cover-sub\s*\{([^}]*)\}", html)
    assert m, "could not extract .cover-sub CSS block"
    css = m.group(1)
    assert "white-space: nowrap" in css or "white-space:nowrap" in css, \
        f".cover-sub must have white-space: nowrap, got: {css!r}"


if __name__ == "__main__":
    tests = [
        test_render_cover_layout_basic,
        test_render_cover_layout_highlight_oob_safe,
        test_cover_fallback_with_hook_word,
        test_cover_fallback_empty_script,
        test_parse_cover_validates_index_bounds,
        test_cover_portrait_orientation,
        test_cover_landscape_orientation,
        test_cover_prompt_requires_hook_not_question,
        test_cover_prompt_sub_no_spoiler,
        test_cover_fallback_finds_hook_sentence,
        test_cover_validate_rejects_first_char_highlight,
        test_cover_validate_rejects_last_char_highlight,
        test_cover_validate_rejects_full_span_highlight,
        test_cover_validate_rejects_question_mark_main,
        test_cover_validate_rejects_sub_spoiler_because,
        test_cover_validate_rejects_sub_spoiler_truth,
        test_cover_subtimes_index_and_time_shift,
        test_cover_validate_rejects_half_word_highlight,
        test_cover_validate_accepts_not_highlight,
        test_cover_validate_rejects_prep_in_highlight,
        test_cover_validate_accepts_number_highlight,
        test_cover_present_first_scene_starts_at_cover_end,
        test_cover_fallback_picks_hook_highlight,
        test_cover_audio_padded_when_delay_set,
        test_cover_no_audio_delay_when_no_cover,
        test_cover_sub_single_line_nowrap,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    if failed:
        print(f"\n{failed}/{len(tests)} failed")
        sys.exit(1)
    print(f"\n{len(tests)}/{len(tests)} passed")
