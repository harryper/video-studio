#!/usr/bin/env python3
"""Unit tests for _enrich_with_kinetic (T11 simplification).

The function must:
  1. Be informational-only — does NOT modify media_items (no type changes,
     no overlay injection, no kinetic items).
  2. Return the same list it was given, in the same order.
  3. Still log scene-type breakdown so the operator can see what got classified.
  4. Have no apply_overlay parameter — that's dead post-cat-doctor.
"""
import logging
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as rj  # noqa: E402


def test_enrich_returns_media_items_unchanged():
    """Output equals input — no type swapping, no overlay injection."""
    items = [
        ("image", Path("/tmp/a.jpg")),
        ("video", Path("/tmp/b.mp4")),
        ("image", Path("/tmp/c.jpg")),
    ]
    chunks = ["第一句有 100 个。", "短句", "末句含数字 50%"]
    out = rj._enrich_with_kinetic(items, chunks, 640, 360)
    assert out == items, f"must return identical list, got {out}"


def test_enrich_does_not_inject_kinetic_items():
    """No new tuples appended; list length is preserved."""
    items = [("image", Path("/tmp/a.jpg"))]
    chunks = ["数字 42%"]
    out = rj._enrich_with_kinetic(items, chunks, 640, 360)
    assert len(out) == 1
    # No overlay or kinetic tags introduced
    kinds = [t[0] for t in out]
    assert "kinetic" not in kinds
    assert "image_overlay" not in kinds
    assert "video_overlay" not in kinds


def test_enrich_accepts_kwargs_without_apply_overlay():
    """Post-cat-doctor there is no apply_overlay parameter."""
    items = [("image", Path("/tmp/a.jpg"))]
    chunks = ["x"]
    # If apply_overlay kwarg still exists, this will raise TypeError.
    try:
        rj._enrich_with_kinetic(items, chunks, 640, 360, apply_overlay=True)
        has_overlay = True
    except TypeError:
        has_overlay = False
    assert not has_overlay, "apply_overlay kwarg must be removed (post-cat-doctor)"


def test_enrich_logs_scene_type_breakdown(caplog=None):
    """Logs the kinetic/scene-type ratio so operators see what got classified."""
    import logging
    items = [
        ("image", Path("/tmp/a.jpg")),
        ("image", Path("/tmp/b.jpg")),
        ("image", Path("/tmp/c.jpg")),
    ]
    chunks = ["100 个", "短句", "末句"]
    with patch.object(rj, "log") as mock_log:
        rj._enrich_with_kinetic(items, chunks, 640, 360)
    # At least one log call mentions "scene classification" or "non-stock".
    calls = [c.args[0] for c in mock_log.call_args_list if c.args]
    assert any("classification" in str(c) or "non-stock" in str(c) for c in calls), \
        f"expected a scene-classification log, got: {calls}"


if __name__ == "__main__":
    test_enrich_returns_media_items_unchanged()
    test_enrich_does_not_inject_kinetic_items()
    test_enrich_accepts_kwargs_without_apply_overlay()
    test_enrich_logs_scene_type_breakdown()
    print("\n✅ all 4 enrich_with_kinetic tests passed")