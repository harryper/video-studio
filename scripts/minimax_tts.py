#!/usr/bin/env python3
"""
MiniMax HTTP TTS for cosmic sleep audio.
Single-call mode: MiniMax supports up to 10,000 chars per request — enough for full ~3800-4500 char scripts.
"""
import argparse, json, os, re, subprocess, sys
from pathlib import Path

API_KEY_FILE = str(Path(__file__).resolve().parent / "minimax_api_key.txt")
DEFAULT_MODEL = "speech-2.8-hd"
# 2026-06-01: 默认音色切换为 Microsoft Azure zh-CN-YunzeNeural 的克隆版。
# voice_id 来源: 使用云泽 4 分钟样本调 https://api.minimaxi.com/v1/voice_clone 克隆得到。
# 注意: 使用云泽合成音频做克隆存在 Azure 神经声音 ToS 风险,仅限内部/授权场景使用。
DEFAULT_VOICE = "azure_yunze_clone"
DEFAULT_SPEED = 0.9
TTS_URL = "https://api.minimaxi.com/v1/t2a_v2"
VOICE_REGISTRY_FILE = Path(__file__).parent / "voice_registry.json"


def load_voice_registry(path=None):
    """Read voice_registry.json; return {} on missing or invalid file."""
    p = Path(path) if path else VOICE_REGISTRY_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_api_key():
    key = os.environ.get("MINIMAX_API_KEY", "")
    if not key and Path(API_KEY_FILE).exists():
        key = Path(API_KEY_FILE).read_text().strip()
    if not key:
        raise SystemExit("MINIMAX_API_KEY not set and " + API_KEY_FILE + " not found")
    return key


def normalize_text(text: str) -> str:
    text = re.sub(r"^#\s*", "", text, flags=re.M)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def char_len(s: str) -> int:
    return len(re.sub(r"\s+", "", s))


def main():
    ap = argparse.ArgumentParser(description="MiniMax HTTP TTS for cosmic sleep audio")
    ap.add_argument("--text", required=True, help="Input text/markdown file")
    ap.add_argument("--out", required=True, help="Output narration audio (mp3)")
    ap.add_argument("--voice", default=DEFAULT_VOICE)
    ap.add_argument("--speed", type=float, default=None,
                    help="Speech speed multiplier. If omitted, falls back to voice_registry default_speed; otherwise to 1.0.")
    ap.add_argument("--voice-registry", default=str(VOICE_REGISTRY_FILE),
                    help="Path to voice_registry.json (for display_name + default_speed lookup).")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=120)
    args = ap.parse_args()

    text = normalize_text(Path(args.text).read_text(encoding="utf-8"))
    if not text:
        raise SystemExit("empty input text")
    chars = char_len(text)
    print(f"Text: {chars} chars", file=sys.stderr)

    # Resolve display_name + default_speed from voice_registry when --voice is a known key.
    registry = load_voice_registry(args.voice_registry)
    voice_meta = registry.get(args.voice, {})
    display_name = voice_meta.get("display_name", args.voice)
    if args.speed is None:
        args.speed = float(voice_meta.get("default_speed", 1.0))
    print(f"Voice: '{display_name}' (id={args.voice}, speed={args.speed})", file=sys.stderr)

    import requests

    api_key = load_api_key()
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    payload = {
        "model": DEFAULT_MODEL,
        "text": text,
        "voice_setting": {
            "voice_id": args.voice,
            "speed": args.speed,
        },
        "audio_setting": {
            "format": "mp3",
            "bitrate": 128000,
        },
    }

    last_err = None
    for attempt in range(1, args.retries + 2):
        try:
            r = requests.post(TTS_URL, headers=headers, json=payload, timeout=args.timeout)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
            resp = r.json()
            audio_hex = resp["data"]["audio"]
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "wb") as f:
                f.write(bytes.fromhex(audio_hex))
            size = out.stat().st_size
            print(f"OK: {size} bytes → {args.out}", file=sys.stderr)
            return
        except Exception as e:
            last_err = e
            if attempt <= args.retries:
                import time
                time.sleep(1.5 * attempt)
    raise SystemExit(f"failed after {args.retries} retries: {last_err}")


if __name__ == "__main__":
    main()
