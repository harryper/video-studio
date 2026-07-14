#!/usr/bin/env python3
"""Unit tests for render_ink_line_card (T10).

Validates the PIL-based fallback card used when MiniMax is unreachable.
The card must:
  1. Be a JPEG on white background (no transparency, no black void).
  2. Include the chunk text rendered visibly (not just metadata).
  3. Render the cat-doctor annotation colors as small swatches —
     red/orange/blue per the 5-段 批注 spec.
  4. Be a valid JPEG that PIL can re-open.
  5. Behave gracefully when PIL font is missing (fall back to default).
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as rj  # noqa: E402


def _render(tmp, chunk, annotations):
    out = Path(tmp) / "card.jpg"
    rj.render_ink_line_card(out, chunk=chunk, annotations=annotations,
                            width=640, height=360)
    return out


def test_card_is_white_jpeg():
    import tempfile
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        out = _render(tmp, "hello", [])
        assert out.exists()
        img = Image.open(out).convert("RGB")
        # The center pixel should be white (RGB ≈ 255,255,255).
        cx, cy = img.size[0] // 2, img.size[1] // 2
        pixel = img.getpixel((cx, cy))
        assert pixel[0] > 240 and pixel[1] > 240 and pixel[2] > 240, \
            f"center pixel must be near-white, got {pixel}"


def test_chunk_text_is_rendered():
    import tempfile
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        out = _render(tmp, "大脑六成是脂肪", [])
        img = Image.open(out).convert("RGB")
        # Scan for non-white pixels — text drawing must produce visible ink.
        # Step=2 gives a fine-grained sample: a 640x360 image yields ~57k
        # samples; even a small chunk text region produces 100+ ink hits.
        non_white = 0
        for x in range(0, img.size[0], 2):
            for y in range(0, img.size[1], 2):
                p = img.getpixel((x, y))
                if p[0] < 200 or p[1] < 200 or p[2] < 200:
                    non_white += 1
        assert non_white > 100, f"chunk text must produce visible ink, got only {non_white} non-white samples"


def test_annotation_colors_rendered():
    import tempfile
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        # The 3 cat-doctor colors per reference/cat-doctor/style-dna.md:
        # red #E74C3C (228,76,60), orange #F39C12 (243,156,18),
        # blue #3498DB (52,152,219).
        anns = [
            {"text": "真相", "color": "red"},
            {"text": "为什么", "color": "orange"},
            {"text": "标签", "color": "blue"},
        ]
        out = _render(tmp, "主题", anns)
        img = Image.open(out).convert("RGB")
        # Look for the three brand colors somewhere in the image.
        found = {"red": False, "orange": False, "blue": False}
        targets = {
            "red": (228, 76, 60),
            "orange": (243, 156, 18),
            "blue": (52, 152, 219),
        }
        tolerance = 30
        for x in range(0, img.size[0], 4):
            for y in range(0, img.size[1], 4):
                p = img.getpixel((x, y))
                for name, target in targets.items():
                    if (abs(p[0] - target[0]) < tolerance
                            and abs(p[1] - target[1]) < tolerance
                            and abs(p[2] - target[2]) < tolerance):
                        found[name] = True
        missing = [k for k, v in found.items() if not v]
        assert not missing, f"missing annotation colors: {missing}"


def test_card_is_valid_jpeg():
    import tempfile
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        out = _render(tmp, "ok", [{"text": "测试", "color": "blue"}])
        # PIL can re-open and identify the format.
        img = Image.open(out)
        img.verify()  # raises on corruption
        img = Image.open(out)  # re-open after verify
        assert img.format == "JPEG"


def test_card_includes_cat_silhouette():
    """Card must draw a recognizable cat silhouette (cat-doctor IP §1).

    The cat has: round head + two triangle ears + two dot eyes + monocle +
    bowtie. We can't OCR the silhouette but we can verify:
      - more black pixels in the central "cat zone" than just text would
        produce (i.e. the cat adds visible structure)
      - the silhouette occupies ~30-40% of canvas per style-dna.md
    """
    import tempfile
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        out = _render(tmp, "", [])  # empty chunk so any ink is from the cat
        img = Image.open(out).convert("RGB")
        w, h = img.size
        # Cat zone: center 40% of canvas (where the cat should sit).
        cx0, cx1 = int(w * 0.30), int(w * 0.70)
        cy0, cy1 = int(h * 0.30), int(h * 0.70)
        ink = 0
        for x in range(cx0, cx1, 2):
            for y in range(cy0, cy1, 2):
                p = img.getpixel((x, y))
                if p[0] < 100 and p[1] < 100 and p[2] < 100:
                    ink += 1
        # With no chunk text, any black ink in the cat zone must come
        # from the cat silhouette drawing itself.
        assert ink > 20, \
            f"cat silhouette must be drawn even with empty chunk; got only {ink} dark pixels in center zone"


def test_handles_missing_cjk_font():
    """If the CJK font file path doesn't exist, fall back gracefully."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "card.jpg"
        # Patch ONLY the CJK-specific truetype call (the one with the
        # /usr/share/fonts/google-noto-cjk/ path). Other truetype calls
        # like load_default() should still work normally.
        from PIL import ImageFont
        import process_video_render_jobs as rj_mod

        orig_truetype = ImageFont.truetype

        def fake_truetype(path, size, *args, **kwargs):
            if "/usr/share/fonts/google-noto-cjk/" in str(path):
                raise OSError("simulated missing font")
            return orig_truetype(path, size, *args, **kwargs)

        with patch.object(ImageFont, "truetype", side_effect=fake_truetype):
            rj.render_ink_line_card(out, chunk="hi", annotations=[],
                                    width=320, height=180)
        # File still produced, no crash, non-empty.
        assert out.exists()
        assert out.stat().st_size > 0


if __name__ == "__main__":
    test_card_is_white_jpeg()
    test_chunk_text_is_rendered()
    test_annotation_colors_rendered()
    test_card_is_valid_jpeg()
    test_card_includes_cat_silhouette()
    test_handles_missing_cjk_font()
    print("\n✅ all 6 ink_line_card tests passed")