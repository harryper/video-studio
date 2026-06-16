#!/usr/bin/env python3
"""Search and download a landscape/portrait Pexels video clip."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_KEY_FILE = Path(__file__).with_name("pexels_api_key.txt")
SEARCH_URL = "https://api.pexels.com/videos/search"


def load_api_key():
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not key and API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("PEXELS_API_KEY is not configured")
    return key


def search(query, per_page=5):
    req = Request(
        f"{SEARCH_URL}?query={quote(query)}&per_page={per_page}",
        headers={"Authorization": load_api_key(), "User-Agent": "voice-studio-pexels/1.0"},
    )
    try:
        with urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8")).get("videos", [])
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Pexels HTTP {exc.code}: {detail[:200]}")
    except URLError as exc:
        raise SystemExit(f"Pexels URL error: {exc}")


def choose_file(videos, width, height):
    landscape = width >= height
    candidates = []
    for video in videos:
        duration = float(video.get("duration") or 0)
        if duration < 4:
            continue
        for item in video.get("video_files", []):
            w = int(item.get("width") or 0)
            h = int(item.get("height") or 0)
            link = item.get("link")
            if not link or not w or not h or item.get("file_type") != "video/mp4":
                continue
            if (w >= h) != landscape:
                continue
            score = abs(w - width) + abs(h - height)
            candidates.append((score, -duration, link))
    return min(candidates, default=(None, None, None))[2]


def download(url, out_path):
    req = Request(url, headers={"User-Agent": "voice-studio-pexels/1.0"})
    with urlopen(req, timeout=90) as response:
        out_path.write_bytes(response.read())


def normalize_video(source_path, out_path, width, height):
    """Create a seek-friendly clip matching the target canvas."""
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
                        help="Rotate starting index for variety (default 0)")
    args = parser.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    videos = search(args.query)
    if args.offset and videos:
        k = args.offset % len(videos)
        videos = videos[k:] + videos[:k]
    link = choose_file(videos, args.w, args.h)
    if not link:
        print(f"NO_RESULT: {args.query}", file=sys.stderr)
        return 1
    raw_path = out_path.with_suffix(out_path.suffix + ".download")
    try:
        download(link, raw_path)
        normalize_video(raw_path, out_path, args.w, args.h)
    finally:
        raw_path.unlink(missing_ok=True)
    if out_path.stat().st_size < 100_000:
        out_path.unlink(missing_ok=True)
        print(f"VIDEO_TOO_SMALL: {args.query}", file=sys.stderr)
        return 1
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
