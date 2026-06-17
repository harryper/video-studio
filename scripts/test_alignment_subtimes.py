#!/usr/bin/env python3
"""Unit tests for _load_alignment_subtimes scene-time handling.

Run: python3 scripts/test_alignment_subtimes.py

Background
----------
Two long-standing bugs in `_load_alignment_subtimes` lived in the
"deferred" section of the README until 2026/06/17. These tests pin
down the fix so the heuristic doesn't drift back.

  1. contained_idx filter (was strict containment):
     The filter `a >= scene_start-0.05 and b <= scene_end+0.05`
     dropped sentences that crossed scene boundaries — a 6s
     sentence starting at scene_end-2s would vanish from BOTH
     scenes' subs even though its middle 4s falls inside scene N
     and its tail 2s falls inside scene N+1. The fix relaxes to
     "overlapping with scene" (`a < scene_end+0.05 and b >
     scene_start-0.05`). Cross-boundary sentences now appear in
     both scenes' contained_idx, and each scene gets the portion
     that falls inside its time range.

  2. Re-clamp last sub to scene_end (was a fill, not a clamp):
     The code checked `last_a < scene_end` and unconditionally set
     the end to scene_end, which is a fill not a clamp — it made
     the last sub display long after the voice had moved on (e.g.
     sentence [0]'s tail persisted to the end of a 10s preview
     even though the next sentence started at 4.88s). The fix
     checks `last_b > scene_end` and only truncates.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as pv  # noqa: E402


def _make_run_dir(alignment: dict, script_text: str) -> tuple[str, Path]:
    """Create a temp run dir with alignment.json + script.txt, return
    (job_id, run_dir_path). Caller is responsible for cleanup."""
    job_id = "test_" + Path(tempfile.mkdtemp(prefix="aln_sub_")).name[-6:]
    run_dir = Path(tempfile.mkdtemp(prefix="aln_sub_run_"))
    (run_dir / "alignment.json").write_text(
        json.dumps(alignment, ensure_ascii=False), encoding="utf-8"
    )
    (run_dir / "script.txt").write_text(script_text, encoding="utf-8")
    return job_id, run_dir


def _cleanup_run_dir(job_id: str, run_dir: Path) -> None:
    """Remove the temp run dir we created. The job_id's run dir is
    SKILL_DIR/runs/<job_id>, which we didn't create — only the temp
    dir holds files we wrote."""
    if run_dir.exists():
        shutil.rmtree(run_dir, ignore_errors=True)


def _two_sentence_alignment() -> tuple[dict, str]:
    """Build a minimal alignment with 2 sentences spanning 0-5s + 5-10s.
    Each sentence has one char so the per-char fallback path in
    _load_alignment_subtimes can compute slot times."""
    script = "第一句。第二句。"
    return {
        "voice_seconds": 10.0,
        "script_chars": len(script),
        "model": "test",
        "word_count": 6,
        "char_count_aligned": 6,
        "sentence_count": 2,
        "sentences": [
            {"text": "第一句。", "start": 0.0, "end": 5.0, "word_indices": [0, 1, 2, 3]},
            {"text": "第二句。", "start": 5.0, "end": 10.0, "word_indices": [4, 5]},
        ],
        "chars": [
            {"c": "第", "start": 0.0, "end": 1.0, "word": "第"},
            {"c": "一", "start": 1.0, "end": 2.0, "word": "一"},
            {"c": "句", "start": 2.0, "end": 3.0, "word": "句"},
            {"c": "。", "start": 3.0, "end": 4.0, "word": "。"},
            {"c": "第", "start": 5.0, "end": 6.0, "word": "第"},
            {"c": "二", "start": 6.0, "end": 7.0, "word": "二"},
            {"c": "句", "start": 7.0, "end": 8.0, "word": "句"},
            {"c": "。", "start": 8.0, "end": 9.0, "word": "。"},
        ],
    }, script


def test_overlapping_sentence_included():
    """A sentence whose span extends past scene_end should still be
    included in contained_idx (and its subs clipped to the scene)."""
    aln, script = _two_sentence_alignment()
    # Sentence [1] spans 5-10s; scene is [0, 7] — sentence overlaps end.
    job_id, run_dir = _make_run_dir(aln, script)
    try:
        # Move run_dir into the path the function expects:
        # SKILL_DIR/runs/<job_id>/. We use a symlink so we don't have
        # to copy files around.
        runs_root = pv.SKILL_DIR / "runs"
        runs_root.mkdir(exist_ok=True)
        link = runs_root / job_id
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(run_dir)
        try:
            chunks = pv.split_script_to_cards(script, n_cards=1)
            # Scene [0, 7] — sentence [0] (0-5) is contained,
            # sentence [1] (5-10) overlaps the end.
            subtimes = pv._load_alignment_subtimes(
                job_id, [(0.0, 7.0)], chunks, width=1920, height=1080,
            )
            assert subtimes is not None
            # Both sentences should be present (overlap is OK now).
            n_subs = sum(len(s) for s in subtimes)
            assert n_subs >= 2, (
                f"expected both sentences' subs; got {n_subs}: "
                f"{[(s[0], s[1], s[2]) for s in subtimes[0]]}"
            )
        finally:
            link.unlink()
    finally:
        _cleanup_run_dir(job_id, run_dir)


def test_re_clamp_truncation_only():
    """When the last sub's actual end < scene_end, do NOT extend it."""
    aln, script = _two_sentence_alignment()
    # Single scene [0, 20] — both sentences fit with room to spare.
    job_id, run_dir = _make_run_dir(aln, script)
    try:
        runs_root = pv.SKILL_DIR / "runs"
        runs_root.mkdir(exist_ok=True)
        link = runs_root / job_id
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(run_dir)
        try:
            chunks = pv.split_script_to_cards(script, n_cards=1)
            subtimes = pv._load_alignment_subtimes(
                job_id, [(0.0, 20.0)], chunks, width=1920, height=1080,
            )
            assert subtimes is not None
            # The last sub's end should be its natural end (≤ 9.0s from
            # chars[7] in the test alignment), NOT extended to 20.0s.
            last_sub = subtimes[0][-1]
            assert last_sub[2] <= 10.0, (
                f"Re-clamp bug: last sub's end was extended to "
                f"{last_sub[2]:.3f}s, expected ≤ 10s (the natural end)"
            )
        finally:
            link.unlink()
    finally:
        _cleanup_run_dir(job_id, run_dir)


def test_re_clamp_actually_truncates():
    """When the last sub's actual end > scene_end, clamp it down."""
    aln, script = _two_sentence_alignment()
    job_id, run_dir = _make_run_dir(aln, script)
    try:
        runs_root = pv.SKILL_DIR / "runs"
        runs_root.mkdir(exist_ok=True)
        link = runs_root / job_id
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(run_dir)
        try:
            chunks = pv.split_script_to_cards(script, n_cards=1)
            # Scene [0, 6] — sentence [1]'s subs extend to ~9s, must clamp.
            subtimes = pv._load_alignment_subtimes(
                job_id, [(0.0, 6.0)], chunks, width=1920, height=1080,
            )
            assert subtimes is not None
            for s_lines, a, b in subtimes[0]:
                assert b <= 6.0, (
                    f"sub {s_lines!r} extends past scene_end: "
                    f"end={b:.3f} > 6.0"
                )
        finally:
            link.unlink()
    finally:
        _cleanup_run_dir(job_id, run_dir)


# ────────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_overlapping_sentence_included,
        test_re_clamp_truncation_only,
        test_re_clamp_actually_truncates,
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
