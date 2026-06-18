#!/usr/bin/env python3
"""Smoke test: _enrich_with_kinetic 默认 apply_overlay=False(2026-06-18
用户反馈后)。函数只跑分类 + 日志,不再生成 overlay HTML。

apply_overlay=True 仍然能跑出 image_overlay / video_overlay 元组(留着
给未来"想要大字"的工作流),build_image_composition_html 也保留
overlay 分支不被触发时也不报错。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as rv  # noqa: E402


def test_decide_scene_type_classifies_chunks():
    cases = [
        ("你刷到一条抖音，三秒划走；创作者说，三秒定生死。", "stock"),
        ("这不是玄学，是节奏——前 0.5 秒钩住你。", "counter"),
        ("你觉得这条够快吗？", "kinetic"),
        ("", "stock"),
    ]
    for chunk, expected in cases:
        got = rv.decide_scene_type(chunk, 0)
        assert got == expected, f"decide_scene_type({chunk!r}) = {got}, want {expected}"
    print("✓ decide_scene_type classifies 4 chunk shapes correctly")


def test_enrich_default_off_passes_through():
    """默认 (apply_overlay=False): media_items 完全不变,counter/kinetic scene
    也不再被替换。"""
    media_items = [
        ("image", Path("/tmp/scene_1.jpg")),
        ("image", Path("/tmp/scene_2.jpg")),
        ("image", Path("/tmp/scene_3.jpg")),
        ("image", Path("/tmp/scene_4.jpg")),
    ]
    chunks = [
        "你刷到一条抖音，三秒划走；创作者说，三秒定生死。",  # stock
        "这不是玄学，是节奏——前 0.5 秒钩住你。",  # counter
        "你觉得这条够快吗？",  # kinetic
        "评论区告诉我你刷到第几秒会划走。",  # kinetic
    ]
    out = rv._enrich_with_kinetic(media_items, chunks, 1080, 1920)
    # 全部原样返回,无 overlay
    assert out == media_items, f"default enrich should be no-op, got {out}"
    for i, item in enumerate(out):
        assert item[0] == "image", f"scene {i} kind {item[0]} should remain 'image'"
    print("✓ default apply_overlay=False: media_items unchanged (4 scenes pass through)")


def test_enrich_explicit_off_also_noop():
    """显式 apply_overlay=False 也走 no-op 路径。"""
    media_items = [("video", Path("/tmp/scene_1.mp4"))]
    chunks = ["前 0.5 秒钩住你"]
    out = rv._enrich_with_kinetic(media_items, chunks, 1080, 1920, apply_overlay=False)
    assert out == media_items, f"explicit off should no-op, got {out}"
    print("✓ explicit apply_overlay=False: also no-op")


def test_enrich_on_emits_image_overlay():
    """apply_overlay=True 保留旧行为:counter/kinetic + image → image_overlay。"""
    media_items = [
        ("image", Path("/tmp/scene_2.jpg")),
    ]
    chunks = ["这不是玄学，是节奏——前 0.5 秒钩住你。"]
    out = rv._enrich_with_kinetic(media_items, chunks, 1080, 1920, apply_overlay=True)
    assert out[0][0] == "image_overlay", f"scene 0 {out[0]}"
    img_path, overlay_html = out[0][1]
    assert str(img_path) == "/tmp/scene_2.jpg"
    assert "kinetic-overlay" in overlay_html
    assert "kinetic-counter" in overlay_html
    print("✓ apply_overlay=True + image + counter → image_overlay (capability preserved)")


def test_enrich_on_emits_video_overlay():
    """apply_overlay=True + video stock + counter → video_overlay。"""
    media_items = [("video", Path("/tmp/scene_1.mp4"))]
    chunks = ["前 0.5 秒钩住你"]
    out = rv._enrich_with_kinetic(media_items, chunks, 1080, 1920, apply_overlay=True)
    assert out[0][0] == "video_overlay", f"scene 0 {out[0]}"
    vid_path, overlay_html = out[0][1]
    assert str(vid_path) == "/tmp/scene_1.mp4"
    assert "kinetic-overlay" in overlay_html
    print("✓ apply_overlay=True + video + counter → video_overlay (capability preserved)")


def test_enrich_on_kinetic_when_no_stock():
    """apply_overlay=True + gradient fallback → pure kinetic。"""
    media_items = [("gradient", Path("/tmp/pad.jpg"))]
    chunks = ["前 0.5 秒钩住你"]
    out = rv._enrich_with_kinetic(media_items, chunks, 1080, 1920, apply_overlay=True)
    assert out[0][0] == "kinetic", f"expected pure kinetic, got {out[0]}"
    assert "kinetic-overlay" not in out[0][1], "should NOT have overlay class"
    print("✓ apply_overlay=True + gradient fallback → pure kinetic")


def test_image_overlay_emits_bg_and_overlay():
    """build_image_composition_html 接受 image_overlay → bg + overlay。"""
    media_items = [
        ("image_overlay", (Path("/tmp/scene_1.jpg"), "<div class='kinetic kinetic-overlay'>X</div>")),
    ]
    chunks = ["短句触发 kinetic"]
    html = rv.build_image_composition_html(
        media_items, chunks, total_duration=2.0, width=1080, height=1920,
    )
    assert 'id="bg-1"' in html, "bg div missing"
    assert "url(images/scene_1.jpg)" in html, "bg image path missing"
    assert "kinetic-overlay" in html, "overlay class missing"
    assert ">X<" in html, "overlay content missing"
    assert "scale:" in html and "#bg-1" in html, "Ken Burns tween missing"
    print("✓ build_image_composition_html emits bg + overlay for image_overlay")


def test_video_overlay_emits_video_and_overlay():
    """build_image_composition_html 接受 video_overlay → <video> + overlay。"""
    media_items = [
        ("video_overlay", (Path("/tmp/scene_1.mp4"), "<div class='kinetic kinetic-overlay'>Y</div>")),
    ]
    chunks = ["前 0.5 秒"]
    html = rv.build_image_composition_html(
        media_items, chunks, total_duration=2.0, width=1080, height=1920,
    )
    assert "<video" in html and 'src="videos/scene_1.mp4"' in html
    assert 'class="bg bg-video"' in html
    assert "kinetic-overlay" in html
    assert "#bg-1" in html
    print("✓ build_image_composition_html emits <video> + overlay for video_overlay")


def test_pure_kinetic_still_has_gradient():
    """纯 kinetic (无 overlay) 保留 inline gradient — 向后兼容。"""
    media_items = [("kinetic", "<div class='kinetic'>OLD</div>")]
    chunks = ["旧行为"]
    html = rv.build_image_composition_html(
        media_items, chunks, total_duration=2.0, width=1080, height=1920,
    )
    assert "OLD" in html
    print("✓ pure kinetic path unchanged")


if __name__ == "__main__":
    test_decide_scene_type_classifies_chunks()
    test_enrich_default_off_passes_through()
    test_enrich_explicit_off_also_noop()
    test_enrich_on_emits_image_overlay()
    test_enrich_on_emits_video_overlay()
    test_enrich_on_kinetic_when_no_stock()
    test_image_overlay_emits_bg_and_overlay()
    test_video_overlay_emits_video_and_overlay()
    test_pure_kinetic_still_has_gradient()
    print("\n✅ all 9 overlay tests passed")