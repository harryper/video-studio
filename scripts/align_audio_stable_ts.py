#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Force-align script text to voice audio using stable-ts (Whisper-based).

Why this exists: TTS-returned word timestamps (MiniMax subtitle_file) are
*predictions* of when the TTS model plans to speak each word. The actual
rendered audio has ms-level drift from those predictions (MP3 encoding,
re-sampling, model decode slack), and the drift compounds across long
scripts — users perceive it as "subs lag behind voice after 20s".

stable-ts runs Whisper's cross-attention alignment against the *actual*
audio waveform, producing per-word timestamps measured from the audio
energy rather than the TTS model's intent.

Output: alignment.json with the SAME schema as the TTS-driven version
(`_build_alignment_from_tts_subs` in process_video_narrate_jobs.py), so
`process_video_render_jobs.py` consumes it without any change.

CLI:
  --voice  path/to/voice.mp3   (required)
  --script path/to/script.txt  (required)
  --out    path/to/alignment.json (required)
  --model  small|medium|large-v3  (default: small)
  --language zh|...  (default: zh)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Avoid a noisy whisper log by lowering progress bars unless we crash.
os.environ.setdefault("TQDM_DISABLE", "1")


def _normalize_script_for_whisper(text: str) -> str:
    """Whisper tokenizer normalizes some Chinese punctuation. Return the
    script as a single line (whisper align() expects plain text)."""
    return " ".join(text.split())  # collapse newlines + extra whitespace


def _merge_decimal_split_sentences(sentences: list[dict]) -> list[dict]:
    """Re-glue sentence pairs that were split mid-decimal.

    The sentence-build loop in `_build_alignment_from_subs` splits on
    `。！？!?.` (see its line 139). The ASCII period `.` is in that set
    because it correctly terminates English sentences ("i.e. 5" →
    "i.e." + "5"), but the same delimiter ALSO severs decimal numbers:
    "前 0.5 秒钩住你" → "前 0." + "5 秒钩住你". The downstream
    preview_caption_ffmpeg path uses sentences[].text verbatim as
    sub-caption text, so a mid-decimal split shows up as "前0." on one
    line and "5秒钩住你" on the next in the rendered mp4.

    Condition for re-glue (intentionally narrow to avoid swallowing
    legitimate English splits):
      sentences[i].text   ends with '.' (or '．' full-width) AND
                          the char immediately before the period is
                          a digit (so the period is a decimal point,
                          not a sentence terminator)
      sentences[i+1].text starts with a digit

    Why this is the right shape: the sentence-build loop splits AT
    the period (the period is the trigger char and stays attached to
    the LEFT fragment), so a decimal split looks like '前0.' / '5秒…',
    not '前0' / '.5秒…'. The `.[-2].isdigit()` check distinguishes
    decimal-point periods (re-glue) from sentence-terminator periods
    like "i.e." (keep split).

    Re-scans from the same index after each merge so chained splits
    like "1.5.5" → ["1.", "5.", "5"] collapse in one pass.

    Returns a NEW list; the input is not mutated. Each merged entry
    concatenates text + word_indices and takes the wider [start, end]
    time span. Stable-ts segment boundaries inside a sentence are
    ignored — the merged entry's timing is the union of both halves,
    which is the right behavior for the consumer (the line displays
    continuously across the silence gap).
    """
    if not sentences:
        return sentences
    merged = list(sentences)
    i = 0
    while i < len(merged) - 1:
        cur_s = merged[i]
        nxt_s = merged[i + 1]
        cur_text = cur_s.get("text", "")
        nxt_text = nxt_s.get("text", "")
        # Decimal split pattern: cur ends with '.' or '．' AND the char
        # before the period is a digit (so the period is a decimal
        # point, not "i.e." or "Dr.") AND next starts with a digit
        # (so it's the continuation of the same decimal number).
        is_decimal_period = (
            len(cur_text) >= 2
            and cur_text[-1] == "."
            and cur_text[-2].isdigit()
        )
        if (cur_text and nxt_text
                and is_decimal_period
                and nxt_text[0].isdigit()):
            merged[i] = {
                "text": cur_text + nxt_text,
                "start": cur_s.get("start", 0.0),
                "end": nxt_s.get("end", 0.0),
                "word_indices": list(cur_s.get("word_indices", []))
                + list(nxt_s.get("word_indices", [])),
            }
            del merged[i + 1]
            continue
        i += 1
    return merged


def _stable_ts_words_to_tts_subs(result) -> list[dict]:
    """Convert a stable_whisper.WhisperResult into the same dict schema
    as voice.subtitle.json (list of segments with timestamped_words).

    Output shape:
      [{"text": "...", "timestamped_words": [
          {"word": "你", "word_begin": 340, "word_end": 460, "time_begin": 340, "time_end": 460},
          ...
      ]}, ...]

    This lets us reuse the script↔word walk logic in
    `_build_alignment_from_tts_subs` by passing the dict through.
    """
    segments_out = []
    for seg in result.segments:
        words = getattr(seg, "words", None) or []
        rows = []
        for w in words:
            word = (getattr(w, "word", "") or "").strip()
            if not word:
                continue
            start_ms = int(round(float(w.start) * 1000))
            end_ms = int(round(float(w.end) * 1000))
            rows.append({
                "word": word,
                "word_begin": start_ms,
                "word_end": end_ms,
                "time_begin": start_ms,
                "time_end": end_ms,
            })
        if not rows:
            continue
        segments_out.append({
            "text": (getattr(seg, "text", "") or "").strip(),
            "timestamped_words": rows,
        })
    return segments_out


def _build_alignment_from_subs(script: str, subs: list[dict], model_tag: str) -> dict:
    """Walk script chars vs flat subtitle words to produce alignment.json.

    Mirrors `process_video_narrate_jobs._build_alignment_from_tts_subs` so
    the rest of the pipeline (preview/render) sees the same schema.
    """
    # Flatten all timestamped_words across segments
    flat_words = []
    for seg in subs:
        for w in seg.get("timestamped_words", []):
            flat_words.append((w["word"], w["time_begin"], w["time_end"]))

    # Build per-char flat list with equal splits inside multi-char words
    flat_chars: list[tuple[str, int, int]] = []
    for w, tb, te in flat_words:
        n = len(w)
        if n == 0:
            continue
        span = te - tb
        for i, ch in enumerate(w):
            c_tb = tb + span * i / n
            c_te = tb + span * (i + 1) / n
            flat_chars.append((ch, c_tb, c_te))

    # Walk script chars in parallel
    char_entries: list[dict] = []
    flat_i = 0
    for sc in script:
        if sc.strip() == "" or sc == "\n":
            continue
        if flat_i >= len(flat_chars):
            break
        ch, tb, te = flat_chars[flat_i]
        if ch != sc:
            matched = None
            for probe in range(flat_i + 1, min(flat_i + 21, len(flat_chars))):
                if flat_chars[probe][0] == sc:
                    matched = probe
                    break
            if matched is not None:
                ch, tb, te = flat_chars[matched]
        char_entries.append({
            "c": sc,
            "start": round(tb / 1000, 3),
            "end": round(te / 1000, 3),
            "word": ch,
        })
        flat_i += 1

    # Build sentence spans from script 。！？
    sentences: list[dict] = []
    cur: list[dict] = []
    next_idx = 0
    for ch in char_entries:
        cur.append(ch)
        if ch["c"] in "。！？!?\.":
            text = "".join(c["c"] for c in cur)
            sentences.append({
                "text": text,
                "start": cur[0]["start"],
                "end": cur[-1]["end"],
                "word_indices": list(range(next_idx, next_idx + len(cur))),
            })
            next_idx += len(cur)
            cur = []
    if cur:
        text = "".join(c["c"] for c in cur)
        sentences.append({
            "text": text,
            "start": cur[0]["start"],
            "end": cur[-1]["end"],
            "word_indices": list(range(next_idx, next_idx + len(cur))),
        })

    # Post-process: merge any pair of adjacent sentences that are
    # obviously two halves of the same decimal number (see
    # _merge_decimal_split_sentences docstring for why this is needed).
    sentences = _merge_decimal_split_sentences(sentences)

    voice_sec = char_entries[-1]["end"] if char_entries else 0.0
    return {
        "voice_seconds": round(voice_sec, 3),
        "script_chars": len(script),
        "model": model_tag,
        "word_count": len([c for c in char_entries if c["word"]]),
        "char_count_aligned": len(char_entries),
        "sentence_count": len(sentences),
        "chars": char_entries,
        "sentences": sentences,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--voice", required=True, help="path to voice.mp3")
    ap.add_argument("--script", required=True, help="path to script.txt (UTF-8)")
    ap.add_argument("--out", required=True, help="path to write alignment.json")
    ap.add_argument("--model", default="small",
                    choices=["tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3"],
                    help="Whisper model size (default: small). CPU inference: "
                         "small≈45s, medium≈2-3min per 60s audio.")
    ap.add_argument("--language", default="zh", help="ISO 639-1 code (default: zh)")
    args = ap.parse_args()

    voice_path = Path(args.voice)
    script_path = Path(args.script)
    out_path = Path(args.out)

    if not voice_path.exists():
        print(f"ERROR: voice file not found: {voice_path}", file=sys.stderr)
        return 1
    if not script_path.exists():
        print(f"ERROR: script file not found: {script_path}", file=sys.stderr)
        return 1

    script_text = script_path.read_text(encoding="utf-8").strip()
    if not script_text:
        print("ERROR: script is empty", file=sys.stderr)
        return 1

    t0 = time.time()
    print(f"[stable-ts] loading model '{args.model}' …", file=sys.stderr)
    import stable_whisper  # local import to keep CLI fast on --help
    model = stable_whisper.load_model(args.model)
    print(f"[stable-ts] model loaded in {time.time()-t0:.1f}s", file=sys.stderr)

    print(f"[stable-ts] aligning {voice_path.name} against script ({len(script_text)} chars) …", file=sys.stderr)
    whisper_text = _normalize_script_for_whisper(script_text)
    result = model.align(str(voice_path), whisper_text, language=args.language)
    print(f"[stable-ts] align done in {time.time()-t0:.1f}s", file=sys.stderr)

    subs = _stable_ts_words_to_tts_subs(result)
    n_words = sum(len(s["timestamped_words"]) for s in subs)
    print(f"[stable-ts] {len(subs)} segments, {n_words} words", file=sys.stderr)

    alignment = _build_alignment_from_subs(script_text, subs, model_tag=f"stable-ts-{args.model}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(alignment, ensure_ascii=False, indent=2), encoding="utf-8")
    size_kb = out_path.stat().st_size // 1024
    print(f"[stable-ts] OK: {out_path} ({size_kb}KB) — voice_seconds={alignment['voice_seconds']:.3f}, "
          f"sentences={alignment['sentence_count']}, chars_aligned={alignment['char_count_aligned']}/{alignment['script_chars']}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
