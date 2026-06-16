#!/usr/bin/env python3
"""Pexels image search + download helper.

Searches Pexels Photos API for a query, returns first matching photo's
URL (cropped to 9:16 vertical for 抖音-style videos).

Free tier: 200 req/hour, 20k req/month. API key required.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_KEY_FILE = Path(__file__).with_name("pexels_api_key.txt")
SEARCH_URL = "https://api.pexels.com/v1/search"
DEFAULT_PER_PAGE = 5
DEFAULT_W, DEFAULT_H = 1080, 1920  # 9:16 vertical for 抖音


def load_api_key():
    key = os.environ.get("PEXELS_API_KEY", "")
    if not key and Path(API_KEY_FILE).exists():
        key = Path(API_KEY_FILE).read_text().strip()
    if not key:
        raise SystemExit("PEXELS_API_KEY not set and " + API_KEY_FILE + " not found")
    return key


def search(query, per_page=DEFAULT_PER_PAGE):
    """Search Pexels Photos. Returns list of dicts with id, alt, src, etc."""
    from urllib.parse import quote
    api_key = load_api_key()
    url = f"{SEARCH_URL}?query={quote(query)}&per_page={per_page}"
    req = Request(url, headers={
        "Authorization": api_key,
        "User-Agent": "Mozilla/5.0 voice-studio-pexels/1.0",
    })
    try:
        with urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Pexels HTTP {e.code}: {body_text[:200]}")
    except URLError as e:
        raise SystemExit(f"Pexels URL error: {e}")
    return body.get("photos", [])


def build_vertical_url(src_dict, w=DEFAULT_W, h=DEFAULT_H):
    """Build a 9:16-cropped URL from a Pexels photo src dict."""
    # Pexels supports custom dimensions via URL params
    base = src_dict.get("original", src_dict.get("large2x", src_dict.get("large", "")))
    if not base:
        return None
    # Strip existing params, add new ones
    base = base.split("?")[0]
    return f"{base}?auto=compress&cs=tinysrgb&w={w}&h={h}&fit=crop"


def download(url, out_path, timeout=30):
    """Download URL to local path. Returns the path."""
    req = Request(url, headers={"User-Agent": "voice-studio-pexels/1.0"})
    with urlopen(req, timeout=timeout) as r:
        with open(out_path, "wb") as f:
            f.write(r.read())
    return out_path


def fetch_one(query, out_path, w=DEFAULT_W, h=DEFAULT_H, offset=0):
    """Search Pexels + download (offset-th) result. Returns (out_path, alt) or (None, error).

    `offset` rotates the starting point in the result list, so different jobs
    / scene indices get different first picks for visual variety. Same offset
    → same pick (idempotent).
    """
    photos = search(query, per_page=3)
    if not photos:
        return None, f"no results for query={query!r}"
    if offset:
        # Rotate so offset-th photo is tried first, then we wrap around
        photos = photos[offset % len(photos):] + photos[:offset % len(photos)]
    for p in photos:
        url = build_vertical_url(p["src"], w, h)
        if not url:
            continue
        try:
            download(url, out_path, timeout=20)
            if out_path.stat().st_size < 5000:
                continue  # too small, try next
            return out_path, p.get("alt", "")
        except Exception as e:
            continue
    return None, f"all {len(photos)} photos failed to download for query={query!r}"


def extract_search_query(chunk_text, theme, scene_index):
    """Extract a 1-3 word Pexels search query from a script chunk.

    Strategy: prefer theme + chunk keywords. Pexels handles Chinese queries
    decently (老人 → 2500+ results), so we just need a coherent search term.
    """
    # Strip punctuation, take first meaningful phrase
    text = re.sub(r"[，。！？、,.!?\s]+", " ", chunk_text).strip()
    # Limit to 4 Chinese characters or 2 English words (Pexels does better with concise)
    words = text.split()[:4]
    base = " ".join(words) if words else (theme or "cinematic")
    # Add scene index hint for variety
    if scene_index == 0:
        return f"{base} wide" if base else "wide shot"
    return base[:30]


def main():
    ap = argparse.ArgumentParser(description="Pexels image search + download for video-studio")
    ap.add_argument("--query", required=True, help="Search query (Chinese or English)")
    ap.add_argument("--out", required=True, help="Output file path (JPEG)")
    ap.add_argument("--w", type=int, default=DEFAULT_W, help="Width (default 1080)")
    ap.add_argument("--h", type=int, default=DEFAULT_H, help="Height (default 1920)")
    ap.add_argument("--per-page", type=int, default=3, help="Photos to try (default 3)")
    ap.add_argument("--offset", type=int, default=0, help="Rotate starting index for variety (default 0)")
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    photos = search(args.query, per_page=args.per_page)
    if args.offset:
        photos = photos[args.offset % len(photos):] + photos[:args.offset % len(photos)]
    for p in photos:
        url = build_vertical_url(p["src"], args.w, args.h)
        if not url:
            continue
        try:
            download(url, out_path)
            if out_path.stat().st_size >= 5000:
                print(str(out_path))
                sys.exit(0)
        except Exception:
            continue
    print(f"NO_RESULT: {args.query}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
