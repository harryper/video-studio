#!/usr/bin/env python3
"""Unit tests for script_length_bounds.

Run: python3 scripts/test_script_length_bounds.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_script_jobs as psj  # noqa: E402


def test_short_floor():
    """durations < ~80s get the 300-char floor, not the 0.7x target ratio."""
    mn30, mx30 = psj.script_length_bounds(30)
    assert mn30 == psj.MIN_SCRIPT_CHARS, f"30s min should be floor 300, got {mn30}"
    mn60, mx60 = psj.script_length_bounds(60)
    assert mn60 == psj.MIN_SCRIPT_CHARS, f"60s min should be floor 300, got {mn60}"
    print("✓ short videos (< ~80s) use 300-char floor")


def test_long_scales_with_duration():
    """For ≥ ~80s, min scales to 70% of target so a long video must
    actually fill its runtime."""
    mn200, mx200 = psj.script_length_bounds(200)
    mn300, mx300 = psj.script_length_bounds(300)
    assert mn300 > mn200, f"300s min ({mn300}) should exceed 200s min ({mn200})"
    assert mx300 > mx200, f"300s max ({mx300}) should exceed 200s max ({mx200})"
    # 300s: target = 300 × 5.4 = 1620, min = int(1620 × 0.7) = 1134
    assert mn300 == 1134, f"300s min expected 1134, got {mn300}"
    # max = int(1620 × 1.3) + 100 = 2206
    assert mx300 == 2206, f"300s max expected 2206, got {mx300}"
    print(f"✓ 200s ({mn200},{mx200}) < 300s ({mn300},{mx300}) scales properly")


def test_300s_1515_chars_passes():
    """The actual v_4c018f91 300s script (1515 chars) must pass — was
    rejected under old 300-1200 cap."""
    mn, mx = psj.script_length_bounds(300)
    assert 1515 >= mn, f"1515 chars below min {mn}"
    assert 1515 <= mx, f"1515 chars above max {mx}"
    print(f"✓ 300s 1515-char script passes (bounds {mn}-{mx})")


def test_under_min_rejected():
    """60s video with only 60 chars is too short — must fail length check."""
    mn, mx = psj.script_length_bounds(60)
    assert not (mn <= 60 <= mx), f"60s 60-char script should be rejected"
    print(f"✓ 60s 60-char script correctly rejected (bounds {mn}-{mx})")


def test_over_max_rejected():
    """60s video with 5000 chars is way too long — must fail."""
    mn, mx = psj.script_length_bounds(60)
    assert not (mn <= 5000 <= mx), f"60s 5000-char script should be rejected"
    print(f"✓ 60s 5000-char script correctly rejected (bounds {mn}-{mx})")


def test_max_grows_with_buffer():
    """max is 1.3× target + 100; for long videos the buffer is small
    relative to target, for short videos it dominates."""
    mn90, mx90 = psj.script_length_bounds(90)
    # 90 × 5.4 = 486, max = 486 × 1.3 + 100 = 631 + 100 = 731
    assert mx90 == 731, f"90s max expected 731, got {mx90}"
    print(f"✓ 90s max = 1.3×486 + 100 = 731")


def main():
    tests = [
        test_short_floor,
        test_long_scales_with_duration,
        test_300s_1515_chars_passes,
        test_under_min_rejected,
        test_over_max_rejected,
        test_max_grows_with_buffer,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())