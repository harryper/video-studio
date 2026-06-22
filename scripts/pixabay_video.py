#!/usr/bin/env python3
"""Search and download a landscape/portrait Pixabay video clip.

Pixabay video response shape (per hit):
  hits[i].videos = {
    "large":  {"url": "...", "width": 1920, "height": 1080, "size": ...},
    "medium": {"url": "...", "width": 1280, "height":  720, "size": ...},
    "small":  {"url": "...", "width":  960, "height":  540, "size": ...},
    "tiny":   {"url": "...", "width":  640, "height":  360, "size": ...},
  }

Tier selection: walk `large → medium → small → tiny`, pick the smallest tier
whose `width >= target_width` AND `size > 0`. This avoids downloading 1920px
footage when 720px is enough.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pixabay_cache  # noqa: E402

API_KEY_FILE = Path(__file__).with_name("pixabay_api_key.txt")
SEARCH_URL = "https://pixabay.com/api/videos/"
MIN_BYTES = 100_000

TIERS = ("tiny", "small", "medium", "large")


def load_api_key():
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key and API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("PIXABAY_API_KEY not set and " + str(API_KEY_FILE) + " not found")
    return key


def search(query, per_page=5):
    api_key = load_api_key()
    params = {
        "lang": "zh",
        "safesearch": "true",
        "per_page": per_page,
        "page": 1,
    }
    key = pixabay_cache.cache_key("video", query, params)
    cached = pixabay_cache.cache_get(key)
    if cached is not None:
        return cached.get("hits", [])

    url = (
        f"{SEARCH_URL}?key={quote(api_key)}"
        f"&q={quote(query)}"
        f"&safesearch=true&lang=zh"
        f"&per_page={per_page}"
    )
    pixabay_cache.rate_limit_acquire()
    req = Request(url, headers={"User-Agent": "voice-studio-pixabay/1.0"})
    try:
        with urlopen(req, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Pixabay HTTP {exc.code}: {detail[:200]}")
    except URLError as exc:
        raise SystemExit(f"Pixabay URL error: {exc}")

    pixabay_cache.cache_set(key, body)
    return body.get("hits", [])


def _choose_video(hit, width, height):
    """Pick smallest-tier video whose dimensions match the target aspect
    and whose declared size is positive. Returns (url, w, h) or (None, 0, 0)."""
    landscape = width >= height
    videos = hit.get("videos") or {}
    for tier in TIERS:
        item = videos.get(tier)
        if not item:
            continue
        url = item.get("url")
        w = int(item.get("width") or 0)
        h = int(item.get("height") or 0)
        size = int(item.get("size") or 0)
        if not url or not w or not h or size <= 0:
            continue
        if (w >= h) != landscape:
            continue
        if w < width:
            continue
        return url, w, h
    return None, 0, 0


def choose_file(hits, width, height):
    """Pick the best tier across all hits. Returns (url, w, h) or (None, 0, 0)."""
    best = (None, 0, 0)
    for hit in hits:
        url, w, h = _choose_video(hit, width, height)
        if url and w > best[1]:
            best = (url, w, h)
    return best


def download(url, out_path):
    req = Request(url, headers={"User-Agent": "voice-studio-pixabay/1.0"})
    with urlopen(req, timeout=90) as response:
        out_path.write_bytes(response.read())


def normalize_video(source_path, out_path, width, height):
    """Create a seek-friendly clip matching the target canvas. Mirrors
    pexels_video.normalize_video so the two backends produce interchangeable
    output."""
    filter_graph = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}"
    )
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(source_path),
            "-an",
            "-vf", filter_graph,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-r", "15",
            "-g", "15",
            "-keyint_min", "15",
            "-sc_threshold", "0",
            "-movflags", "+faststart",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[-800:])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--w", type=int, default=1920)
    parser.add_argument("--h", type=int, default=1080)
    parser.add_argument("--offset", type=int, default=0,
                        help="Rotate starting hit for variety")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    hits = search(args.query)
    if args.offset and hits:
        k = args.offset % len(hits)
        hits = hits[k:] + hits[:k]
    link, _src_w, _src_h = choose_file(hits, args.w, args.h)
    if not link:
        print(f"NO_RESULT: {args.query}", file=sys.stderr)
        return 1
    raw_path = out_path.with_suffix(out_path.suffix + ".download")
    try:
        download(link, raw_path)
        normalize_video(raw_path, out_path, args.w, args.h)
    finally:
        raw_path.unlink(missing_ok=True)
    if out_path.stat().st_size < MIN_BYTES:
        out_path.unlink(missing_ok=True)
        print(f"VIDEO_TOO_SMALL: {args.query}", file=sys.stderr)
        return 1
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())