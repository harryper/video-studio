#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch per-word timestamps from MiniMax TTS API without producing audio.

Why this exists: minimax_tts.py (in scripts/) is the canonical TTS
client. We don't want to modify it for word-level needs — instead we
make a second HTTP call to the same endpoint asking only for word-level
subtitles, then download the subtitle_file JSON. Audio comes from the
regular minimax_tts.py call; subtitles come from this script. The two
are independent and a failure in one doesn't affect the other.

Request body adds:
    subtitle_enable: true
    subtitle_type: "word"   (sentence / word / word_streaming)

Response has data.subtitle_file (a presigned OSS URL). Downloaded JSON
is a list of segments, each with timestamped_words:
    [{word, word_begin, word_end, time_begin(ms), time_end(ms), ...}, ...]

The full text per segment is in segment.text. TTS strips 。！？ punctuation
from timestamped_words but keeps it in segment.text — the alignment
converter in process_video_narrate_jobs re-attaches punctuation by walking
the original script.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

API_KEY_FILE = Path(__file__).resolve().parent / "minimax_api_key.txt"
TTS_URL = "https://api.minimaxi.com/v1/t2a_v2"
DEFAULT_MODEL = "speech-2.8-hd"
DEFAULT_TIMEOUT = 120


def load_api_key() -> str:
    if not API_KEY_FILE.exists():
        raise SystemExit(f"MINIMAX API key file not found: {API_KEY_FILE}")
    key = API_KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(f"MINIMAX API key file is empty: {API_KEY_FILE}")
    return key


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch per-word timestamps from MiniMax TTS")
    ap.add_argument("--text", required=True, help="Path to UTF-8 script text file")
    ap.add_argument("--out", required=True, help="Path to write downloaded subtitle JSON")
    ap.add_argument("--voice", default="", help="voice_id; empty = server default")
    ap.add_argument("--speed", type=float, default=1.0, help="Speech speed multiplier")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"TTS model name (default: {DEFAULT_MODEL})")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout (s)")
    args = ap.parse_args()

    text_path = Path(args.text)
    out_path = Path(args.out)
    if not text_path.exists():
        raise SystemExit(f"text file not found: {text_path}")
    text = text_path.read_text(encoding="utf-8").strip()
    if not text:
        raise SystemExit("text file is empty")

    api_key = load_api_key()

    payload = {
        "model": args.model,
        "text": text,
        "voice_setting": {
            "voice_id": args.voice,
            "speed": args.speed,
        },
        "audio_setting": {
            "format": "mp3",
            "bitrate": 128000,
        },
        "subtitle_enable": True,
        "subtitle_type": "word",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    import requests
    r = requests.post(TTS_URL, headers=headers, json=payload, timeout=args.timeout)
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:500]}", file=sys.stderr)
        return 1
    resp = r.json()
    if resp.get("base_resp", {}).get("status_code") not in (0, None):
        print(f"API error: {resp.get('base_resp')}", file=sys.stderr)
        return 2

    sub_url = (resp.get("data") or {}).get("subtitle_file", "")
    if not sub_url:
        print(
            f"WARN: subtitle_type=word requested but no subtitle_file in response. "
            f"keys={list((resp.get('data') or {}).keys())}",
            file=sys.stderr,
        )
        return 3

    out_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(sub_url, headers={"User-Agent": "video-studio/1.0"})
    with urllib.request.urlopen(req, timeout=30) as fh:
        raw = fh.read()
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        print(f"subtitle JSON parse error: {e}; first 200 bytes: {raw[:200]!r}", file=sys.stderr)
        return 4

    out_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")

    n_seg = len(parsed) if isinstance(parsed, list) else 0
    n_words = sum(len(s.get("timestamped_words", [])) for s in parsed) if isinstance(parsed, list) else 0
    print(f"OK: {out_path} — {n_seg} segments, {n_words} words", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
