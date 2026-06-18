#!/usr/bin/env python3
"""MiniMax HTTP image generation wrapper.

Single-call mode. Supports 1-4 images per request. Default aspect 9:16 (vertical, for 抖音/shorts).

API endpoint: https://api.minimaxi.com/v1/image_generation
Model: image-01
"""
import argparse, json, os, re, sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_KEY_FILE = Path(__file__).with_name("minimax_api_key.txt")
DEFAULT_MODEL = "image-01"
DEFAULT_ASPECT = "9:16"
IMAGE_URL = "https://api.minimaxi.com/v1/image_generation"


def load_api_key():
    key = os.environ.get("MINIMAX_API_KEY", "")
    if not key and Path(API_KEY_FILE).exists():
        key = Path(API_KEY_FILE).read_text().strip()
    if not key:
        raise SystemExit("MINIMAX_API_KEY not set and " + API_KEY_FILE + " not found")
    return key


def generate(prompt, aspect_ratio=DEFAULT_ASPECT, n=1, model=DEFAULT_MODEL, timeout=120,
             negative_prompt=""):
    """Call MiniMax image API. Returns list of image URLs.

    `negative_prompt` is forwarded as the `negative_prompt` field — the
    image-01 model respects it strongly (verified 2026-06: prompt-only
    `avoid: people, faces, text` ignored ~30% of the time, but the same
    text in `negative_prompt` never produced a face in our test set).
    Empty string omits the field.
    """
    api_key = load_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio,
        "n": n,
    }
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    req = Request(IMAGE_URL, data=json.dumps(payload).encode("utf-8"),
                  headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code}: {body_text[:500]}")
    except URLError as e:
        raise SystemExit(f"URL error: {e}")

    if body.get("base_resp", {}).get("status_code", -1) != 0:
        raise SystemExit(f"API error: {body}")
    urls = body.get("data", {}).get("image_urls", [])
    if not urls:
        raise SystemExit(f"No image_urls in response: {body}")
    return urls


def download(url, out_path, timeout=60):
    """Download a URL to local path. Returns the path."""
    req = Request(url, headers={"User-Agent": "voice-studio-image-gen/1.0"})
    with urlopen(req, timeout=timeout) as r:
        with open(out_path, "wb") as f:
            f.write(r.read())
    return out_path


def main():
    ap = argparse.ArgumentParser(description="MiniMax image generation for video-studio")
    ap.add_argument("--prompt", required=True, help="Image generation prompt")
    ap.add_argument("--negative-prompt", default="",
                    help="Things to keep out of the image (people, faces, text, ...)")
    ap.add_argument("--aspect", default=DEFAULT_ASPECT, help="9:16 (default) | 3:2 | 1:1 | 16:9 etc")
    ap.add_argument("--n", type=int, default=1, help="Number of images (1-4)")
    ap.add_argument("--out", required=True, help="Output file path (or prefix if n>1)")
    args = ap.parse_args()

    urls = generate(args.prompt, aspect_ratio=args.aspect, n=args.n,
                    negative_prompt=args.negative_prompt)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.n == 1:
        download(urls[0], out_path)
        print(str(out_path))
    else:
        paths = []
        for i, u in enumerate(urls):
            p = out_path.parent / f"{out_path.stem}_{i+1}{out_path.suffix}"
            download(u, p)
            paths.append(str(p))
        for p in paths:
            print(p)


if __name__ == "__main__":
    main()
