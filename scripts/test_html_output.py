#!/usr/bin/env python3
"""Smoke test: call build_image_composition_html with a real script and
verify the resulting HTML has the expected sub-caption structure.

This bypasses the slow Pexels image fetch / hyperframes render pipeline
and only validates the HTML composition — the part we just changed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_video_render_jobs as rv  # noqa: E402

SCRIPT = (
    "video-studio 拆分跑完了。你以为这就叫上线？错。"
    "95% 的拆分，死在最后一公里。"
    "前面 14 天搭骨架、调接口、过联调，全过。"
    "第十六天上线，老板点开页面，黑屏。"
    "第 0 秒路由 200，第 1 秒转码没起来，第 3 秒回源 404，第 5 秒前端弹堆栈。"
    "拆分不是改完代码，拆分是改完还能用。"
    "这次验证一共跑了五关。哪五关？转码、切片、签名、回源、播放。"
    "每一关单独跑都能过，五关串起来就崩。"
    "这种 bug，单元测试抓不到，联调也抓不到，端到端走完才会冒头。"
    "第一关，转码。喂一个 14 分钟的 mp4 进去，看时长、看码率、看音轨。"
    "时长超 14 分 02 秒，pass。码率稳在 320k，pass。音轨不是立体声，fail。"
    "第二关，切片。产物切成 ts，索引写成 m3u8。"
    "点开 m3u8 看一行：EXT-X-VERSION 必须是 3，TARGETDURATION 必须是 6 秒。"
    "切片失败的样本大多死在版本号上。"
    "第三关，签名。URL 后面那一串 query，30 秒过期。改一个字符，回源 403。"
    "签名前缀不能写死，必须从配置中心拉。否则一上线就 403。"
    "第四关，回源。CDN miss，回源到对象存储。第一次 200，缓存命中后 304。"
    "错一次，CDN 配置就要重写一遍，这种坑藏得最深。"
    "第五关，播放。打开前端，点播放。"
    "前四关是手段，第五关才是目的。"
    "前四关全过、第五关黑屏，等于零。"
)

# Mimic the render daemon's preprocessing: chunk into N scenes
n_scenes = 15
chunks = rv.split_script_to_cards(SCRIPT, n_cards=n_scenes)
print(f"=== {len(SCRIPT)} chars / {n_scenes} scenes ({len(chunks)} chunks) ===\n")
for i, c in enumerate(chunks):
    subs = rv.wrap_to_subcaptions(c, max_chars=18, max_lines=2)
    print(f"scene-{i+1:02d} ({len(c):2d} chars, {len(subs)} sub-cap):")
    for j, sub in enumerate(subs):
        for k, line in enumerate(sub):
            print(f"    sub-{j+1} L{k+1}: {line}")
    print()

# Now build the full HTML and verify sub-caption structure
print("\n=== HTML generation smoke test ===")
# Fake media_items for the build (won't be rendered, just need to exist)
fake_media = [("image", Path(f"/tmp/scene_{i+1}.jpg")) for i in range(n_scenes)]

# Patch the function to skip Pexels (we use fake paths)
html = rv.build_image_composition_html(
    fake_media,
    chunks,
    total_duration=150.0,
    width=1920,
    height=1080,
)

# Count sub-caption divs
import re
sub_ids = re.findall(r'id="(sub-\d+-\d+)"', html)
print(f"Total sub-caption divs in HTML: {len(sub_ids)}")
print(f"  per-scene counts: ", end="")
from collections import Counter
per_scene = Counter(s.split("-")[1] for s in sub_ids)
print(dict(sorted(per_scene.items(), key=lambda x: int(x[0]))))

# Check that the opening hook timeline is followed by sub-caption fade-in
hook_fade_out = re.search(r"'#opening-hook'.*?opacity: 0.*?3\.8", html)
print(f"Opening hook fades out at 3.8s: {bool(hook_fade_out)}")

# Check that sub-1-1 (scene 1's first sub) fade-in is at ~3.8s, not 0
sub_1_1_fadein = re.search(r"'#sub-1-1'.*?opacity: 0.*?opacity: 1.*?duration: 0\.2.*?,\s*([\d.]+)\)", html)
if sub_1_1_fadein:
    print(f"  sub-1-1 fade-in at: {sub_1_1_fadein.group(1)}s (should be 3.8, not 0)")
else:
    print("  sub-1-1 fade-in: not found via regex")

# Check that Ken Burns uses power1.inOut (not 'none')
kb_ease = re.search(r"tl\.to\('#bg-1'.*?ease:\s*'([^']+)'", html)
print(f"Ken Burns ease on bg-1: {kb_ease.group(1) if kb_ease else 'not found'}")

# Check kb_variants magnitudes
import re
scales = [float(m) for m in re.findall(r"scale:\s*([\d.]+)", html)][:n_scenes]
print(f"Ken Burns scales (first {n_scenes}): min={min(scales):.2f}, max={max(scales):.2f}")

# Save the HTML for visual inspection
out = Path("/tmp/test_composition.html")
out.write_text(html, encoding="utf-8")
print(f"\nFull HTML written to {out} ({len(html)} bytes)")
