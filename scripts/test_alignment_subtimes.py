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


def _multi_clause_alignment() -> tuple[dict, str]:
    """Build an alignment with 2 long sentences, both crossing scene
    boundaries. Each sentence is > max_chars so _split_sentence_into_subs
    splits at PUNCT boundaries into multiple subs.

    Sentence [0] (4 PUNCT clauses at 0-9s, 8 subs after split):
      "前段,中段,后段,末段,第五段,第六段,第七段,第八段。"
      → 8 subs at roughly 1.125s each, straddle scene boundary at 5s
        (subs 0-3 in scene [0, 5], subs 4-7 in scene [5, 9]).
    Sentence [1] (1 PUNCT at 9-12s):
      "第一句。"
    """
    script = "前段,中段,后段,末段,第五段,第六段,第七段,第八段。第一句。"
    n = len(script)
    sent0_text = "前段,中段,后段,末段,第五段,第六段,第七段,第八段。"
    sent1_text = "第一句。"
    sent0_dur = 9.0
    sent1_start = sent0_dur
    sent1_dur = 3.0
    sent0_step = sent0_dur / len(sent0_text)
    sent1_step = sent1_dur / len(sent1_text)
    char_entries = []
    t = 0.0
    for i, c in enumerate(script):
        if i == len(sent0_text):
            t = sent1_start
        dur = sent0_step if i < len(sent0_text) else sent1_step
        char_entries.append({"c": c, "start": round(t, 3), "end": round(t + dur, 3), "word": c})
        t += dur
    return {
        "voice_seconds": sent0_dur + sent1_dur,
        "script_chars": n,
        "model": "test",
        "word_count": n,
        "char_count_aligned": n,
        "sentence_count": 2,
        "sentences": [
            {"text": sent0_text, "start": 0.0, "end": sent0_dur,
             "word_indices": list(range(len(sent0_text)))},
            {"text": sent1_text, "start": sent1_start, "end": sent1_start + sent1_dur,
             "word_indices": list(range(len(sent0_text), n))},
        ],
        "chars": char_entries,
    }, script


def test_subs_clipped_to_scene_boundaries():
    """For a multi-clause sentence straddling the scene boundary, every
    sub in every scene must have slot_start >= scene_start AND slot_end
    <= scene_end. Earlier code only clamped the LAST sub's end; the
    SUB_GAP first-sub clamp coincidentally fixed short sentences but
    leaves multi-clause sentences with subs that violate scene bounds.

    Also asserts no two scenes share the exact same sub text (the
    "two scenes each show a segment of the same sentence" duplication
    bug the user reported — when sentence [0] is 9s long and crosses
    a 5s boundary, both scene[0] and scene[1] would currently show all
    8 of its subs, with text repeated in both scenes).
    """
    aln, script = _multi_clause_alignment()
    scene_times = [(0.0, 5.0), (5.0, 9.0), (9.0, 12.0)]
    job_id, run_dir = _make_run_dir(aln, script)
    try:
        runs_root = pv.SKILL_DIR / "runs"
        runs_root.mkdir(exist_ok=True)
        link = runs_root / job_id
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(run_dir)
        try:
            chunks = pv.split_script_to_cards(script, n_cards=3)
            subtimes = pv._load_alignment_subtimes(
                job_id, scene_times, chunks, width=1920, height=1080,
            )
            assert subtimes is not None
            assert len(subtimes) == len(scene_times)
            for scene_i, (s_start, s_end) in enumerate(scene_times):
                scene_subs = subtimes[scene_i]
                for s_lines, a, b in scene_subs:
                    assert a >= s_start - 0.001, (
                        f"scene[{scene_i}] sub {s_lines!r} starts before "
                        f"scene_start: start={a:.3f} < scene_start={s_start:.3f}"
                    )
                    assert b <= s_end + 0.001, (
                        f"scene[{scene_i}] sub {s_lines!r} ends past "
                        f"scene_end: end={b:.3f} > scene_end={s_end:.3f}"
                    )
            # No text overlap across scenes: a sub's first-line text
            # should not appear in two scenes' sub lists.
            seen: dict[str, int] = {}
            for scene_i, scene_subs in enumerate(subtimes):
                for s_lines, _, _ in scene_subs:
                    key = s_lines[0] if s_lines else ""
                    if not key:
                        continue
                    if key in seen and seen[key] != scene_i:
                        raise AssertionError(
                            f"sub text {key!r} appears in both scene "
                            f"{seen[key]} and scene {scene_i} — cross-scene "
                            f"duplicate (the 'both scenes show same text' bug)"
                        )
                    seen[key] = scene_i
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
        test_subs_clipped_to_scene_boundaries,
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
