#!/usr/bin/env python3
"""Unit tests for _load_alignment_scene_times pad-scene handling.

Run: python3 scripts/test_alignment_scene_times.py

Background
----------
When `n_scenes` (computed from total_duration) is greater than the
number of sentences in script.txt, `split_script_to_cards` pads the
chunk list with empty strings to reach n_scenes. Those empty chunks
have no alignment match, so `_load_alignment_scene_times` used to
return (0.0, 0.0) for them. That fed into
`build_image_composition_html`'s `starts` list and reset the cumulative
timeline to 0, turning per_this for every scene between the last real
chunk and the pad into a negative number — and the rendered HTML ended
up with `<div ... data-start="12.38" data-duration="-12.38">`, which
hyperframes lint flags as `overlapping_clips_same_track` because the
clip's data-end lands at 0, colliding with the first real scene.

The fix:

  1. `_load_alignment_scene_times` returns `None` for empty chunks
     (instead of `(0.0, 0.0)`), and only treats `None` from non-empty
     chunks as "real chunk missed, fall back to equal-time". Pad None
     values stay in the list and are handled downstream.
  2. `_load_alignment_subtimes` skips None scene spans (they have no
     chunk text to wrap, so no subs to emit).
  3. `build_image_composition_html` skips pad scenes (empty chunk) in
     the per-scene loop, so they don't get a `<div class="clip">` and
     can't trigger the overlap lint.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as pv  # noqa: E402


def _make_run_dir(alignment: dict, script_text: str) -> tuple[str, Path]:
    """Mirror of test_alignment_subtimes helper — symlink the temp dir
    into SKILL_DIR/runs/<job_id> so _load_alignment_scene_times can
    find alignment.json via the production path."""
    job_id = "test_" + Path(tempfile.mkdtemp(prefix="aln_scn_")).name[-6:]
    run_dir = Path(tempfile.mkdtemp(prefix="aln_scn_run_"))
    (run_dir / "alignment.json").write_text(
        json.dumps(alignment, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "script.txt").write_text(script_text, encoding="utf-8")
    runs_root = pv.SKILL_DIR / "runs"
    runs_root.mkdir(exist_ok=True)
    link = runs_root / job_id
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(run_dir)
    return job_id, run_dir, link


def _cleanup_run_dir(job_id: str, run_dir: Path, link: Path) -> None:
    if link.exists() or link.is_symlink():
        link.unlink()
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)


def _four_sentence_alignment() -> tuple[dict, str]:
    """4 sentences spanning 0-14.44s, mimicking v_5dec0abc."""
    script = "你刷到一条抖音，三秒划走；创作者说，三秒定生死。这不是玄学，是节奏——前 0.5 秒钩住你，中间每 1.5 秒一个新刺激。你觉得这条够快吗？评论区告诉我你刷到第几秒会划走。"
    return {
        "voice_seconds": 14.44,
        "script_chars": len(script),
        "model": "test",
        "word_count": 4,
        "char_count_aligned": len(script),
        "sentence_count": 4,
        "sentences": [
            {"text": "你刷到一条抖音，三秒划走；创作者说，三秒定生死。",
             "start": 0.34, "end": 4.00, "word_indices": [0]},
            {"text": "这不是玄学，是节奏——前 0.5 秒钩住你，中间每 1.5 秒一个新刺激。",
             "start": 4.88, "end": 10.67, "word_indices": [1]},
            {"text": "你觉得这条够快吗？",
             "start": 10.94, "end": 11.90, "word_indices": [2]},
            {"text": "评论区告诉我你刷到第几秒会划走。",
             "start": 12.38, "end": 14.44, "word_indices": [3]},
        ],
    }, script


def test_pad_chunks_return_none():
    """Empty chunks from split_script_to_cards trailing-pad should be
    marked None in scene_times, not (0.0, 0.0)."""
    aln, script = _four_sentence_alignment()
    job_id, run_dir, link = _make_run_dir(aln, script)
    try:
        # 4 sentences, n_scenes=15 → 11 empty pad chunks
        chunks = pv.split_script_to_cards(script, n_cards=15)
        n_pad = sum(1 for c in chunks if not c)
        assert n_pad == 11, f"expected 11 pad chunks, got {n_pad}"
        scene_times = pv._load_alignment_scene_times(job_id, chunks, 10.0)
        assert scene_times is not None and len(scene_times) == 15
        # First 4 are real, last 11 are None (pads)
        assert all(t is not None for t in scene_times[:4]), \
            f"first 4 should be real, got {scene_times[:4]}"
        assert all(t is None for t in scene_times[4:]), \
            f"trailing pads should be None, got {scene_times[4:]}"
    finally:
        _cleanup_run_dir(job_id, run_dir, link)


def test_starts_monotonic_with_pads():
    """The starts list built by build_image_composition_html must be
    non-decreasing — pad scenes should not reset the timeline cursor
    back to 0 (the original bug that produced negative per_this)."""
    aln, script = _four_sentence_alignment()
    job_id, run_dir, link = _make_run_dir(aln, script)
    try:
        chunks = pv.split_script_to_cards(script, n_cards=15)
        scene_times = pv._load_alignment_scene_times(job_id, chunks, 10.0)
        subtimes = pv._load_alignment_subtimes(
            job_id, scene_times, chunks, width=1920, height=1080,
        )
        media_items = [("image", f"images/scene_{i+1}.jpg") for i in range(15)]
        html = pv.build_image_composition_html(
            media_items, chunks, total_duration=10.0, width=1920, height=1080,
            scene_times=scene_times, subtimes=subtimes,
        )
        # Pull data-start and data-duration for every scene-N clip.
        import re
        clips = re.findall(
            r'<div id="scene-(\d+)" class="clip" '
            r'data-track-index="1" data-start="([\d.\-]+)" '
            r'data-duration="([\d.\-eE]+)">',
            html,
        )
        assert len(clips) == 4, (
            f"expected 4 real scene clips (pads skipped), got {len(clips)}: {clips}"
        )
        # Each clip's data-end = data-start + data-duration must be > 0
        # and the per_this must be non-negative.
        for n, s, d in clips:
            start = float(s)
            dur = float(d)
            end = start + dur
            assert end > 0, (
                f"scene-{n}: data-end={end} ≤ 0 (start={start}, dur={dur})"
            )
            assert dur >= 0, (
                f"scene-{n}: negative duration {dur}"
            )
        # Starts must be strictly increasing (clips don't overlap).
        starts = [float(s) for _, s, _ in clips]
        for prev, nxt in zip(starts, starts[1:]):
            assert nxt > prev, (
                f"non-monotonic starts: {prev} → {nxt}"
            )
    finally:
        _cleanup_run_dir(job_id, run_dir, link)


def test_subtimes_skip_pad_scenes():
    """_load_alignment_subtimes must handle None scene spans (from pad
    chunks) without crashing. Each None produces an empty subs list."""
    aln, script = _four_sentence_alignment()
    job_id, run_dir, link = _make_run_dir(aln, script)
    try:
        chunks = pv.split_script_to_cards(script, n_cards=15)
        scene_times = pv._load_alignment_scene_times(job_id, chunks, 10.0)
        subtimes = pv._load_alignment_subtimes(
            job_id, scene_times, chunks, width=1920, height=1080,
        )
        assert subtimes is not None and len(subtimes) == 15
        # Real scenes have subs, pad scenes have empty lists.
        for i, t in enumerate(scene_times):
            if t is None:
                assert subtimes[i] == [], \
                    f"pad scene {i} should have empty subs, got {subtimes[i]}"
            else:
                assert len(subtimes[i]) > 0, \
                    f"real scene {i} should have subs, got empty"
    finally:
        _cleanup_run_dir(job_id, run_dir, link)


# ────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_pad_chunks_return_none,
        test_starts_monotonic_with_pads,
        test_subtimes_skip_pad_scenes,
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
