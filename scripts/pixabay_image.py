#!/usr/bin/env python3
"""Pixabay image search + download helper.

Pixabay API rules (https://pixabay.com/api/docs/):
  - key in URL query, NOT Authorization header
  - 100 req / 60s — rate-limited via pixabay_cache.rate_limit_acquire
  - 24h cache required — wrapper checks cache before urlopen
  - Image hotlinking forbidden — wrapper downloads to local

Field choices (free tier key):
  - largeImageURL (1280px) is the highest-res reliably populated field
  - webformatURL (640px) is the universal fallback
  - imageURL (full) is only present with "full API access" — DO NOT use
  - previewURL (150px) is too small for 1920x1080 canvas
"""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pixabay_cache  # noqa: E402

API_KEY_FILE = Path(__file__).with_name("pixabay_api_key.txt")
SEARCH_URL = "https://pixabay.com/api/"
DEFAULT_PER_PAGE = 5
DEFAULT_W, DEFAULT_H = 1920, 1080
MIN_BYTES = 5000


def load_api_key():
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key and API_KEY_FILE.exists():
        key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit("PIXABAY_API_KEY not set and " + str(API_KEY_FILE) + " not found")
    return key


def search(query, per_page=DEFAULT_PER_PAGE, orientation="horizontal"):
    """Search Pixabay Photos. Returns list of hit dicts (cached 24h)."""
    api_key = load_api_key()
    params = {
        "lang": "zh",
        "safesearch": "true",
        "image_type": "photo",
        "orientation": orientation,
        "per_page": per_page,
        "page": 1,
    }
    key = pixabay_cache.cache_key("image", query, params)
    cached = pixabay_cache.cache_get(key)
    if cached is not None:
        return cached.get("hits", [])

    url = (
        f"{SEARCH_URL}?key={quote(api_key)}"
        f"&q={quote(query)}"
        f"&image_type=photo&safesearch=true&lang=zh"
        f"&orientation={orientation}"
        f"&per_page={per_page}"
    )
    pixabay_cache.rate_limit_acquire()
    req = Request(url, headers={"User-Agent": "voice-studio-pixabay/1.0"})
    try:
        with urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Pixabay HTTP {e.code}: {body_text[:200]}")
    except URLError as e:
        raise SystemExit(f"Pixabay URL error: {e}")

    pixabay_cache.cache_set(key, body)
    return body.get("hits", [])


def _pick_url(hit):
    """Prefer 1280px large, fall back to 640px webformat. Skip preview (150px)."""
    return hit.get("largeImageURL") or hit.get("webformatURL")


def download(url, out_path, timeout=30):
    req = Request(url, headers={"User-Agent": "voice-studio-pixabay/1.0"})
    with urlopen(req, timeout=timeout) as r:
        with open(out_path, "wb") as f:
            f.write(r.read())


def fetch_one(query, out_path, w=DEFAULT_W, h=DEFAULT_H, offset=0):
    """Search + download (offset-th rotated) image. Returns (out_path, alt) or (None, err)."""
    if w >= h:
        orientation = "horizontal"
    else:
        orientation = "vertical"
    hits = search(query, per_page=3, orientation=orientation)
    if not hits:
        return None, f"no results for query={query!r}"
    if offset:
        hits = hits[offset % len(hits):] + hits[:offset % len(hits)]
    for hit in hits:
        url = _pick_url(hit)
        if not url:
            continue
        try:
            download(url, out_path, timeout=20)
            if out_path.stat().st_size < MIN_BYTES:
                continue
            return out_path, hit.get("tags", "")
        except Exception:
            continue
    return None, f"all {len(hits)} hits failed to download for query={query!r}"


def main():
    ap = argparse.ArgumentParser(description="Pixabay image search + download for video-studio")
    ap.add_argument("--query", required=True, help="Search query")
    ap.add_argument("--out", required=True, help="Output file path (JPEG)")
    ap.add_argument("--w", type=int, default=DEFAULT_W, help="Width (default 1920)")
    ap.add_argument("--h", type=int, default=DEFAULT_H, help="Height (default 1080)")
    ap.add_argument("--per-page", type=int, default=3, help="Hits to try (default 3)")
    ap.add_argument("--offset", type=int, default=0, help="Rotate starting index for variety")
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.w >= args.h:
        orientation = "horizontal"
    else:
        orientation = "vertical"
    hits = search(args.query, per_page=args.per_page, orientation=orientation)
    if args.offset and hits:
        hits = hits[args.offset % len(hits):] + hits[:args.offset % len(hits)]
    for hit in hits:
        url = _pick_url(hit)
        if not url:
            continue
        try:
            download(url, out_path)
            if out_path.stat().st_size >= MIN_BYTES:
                print(str(out_path))
                sys.exit(0)
        except Exception:
            continue
    print(f"NO_RESULT: {args.query}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()