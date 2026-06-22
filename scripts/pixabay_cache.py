#!/usr/bin/env python3
"""Pixabay cache + sliding-window rate limiter.

Pixabay API rules (https://pixabay.com/api/docs/):
  - 100 req / 60s per key — HTTP 429 when exceeded
  - 24h cache required by ToS
  - Image hotlinking forbidden (CDN URLs expire in 24h)

This module is the policy layer; pixabay_image.py / pixabay_video.py call
into it before any urlopen and after any successful response.

Layout:
  ~/.cache/pixabay/<sha256-key>.json     # cached API responses (24h TTL)
  ~/.cache/pixabay/.rate/requests.jsonl  # sliding-window timestamps
  ~/.cache/pixabay/.rate/rate.lock       # fcntl.flock gate
"""
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path

CACHE_DIR = Path("~/.cache/pixabay").expanduser()
RATE_DIR = CACHE_DIR / ".rate"
RATE_LOG = RATE_DIR / "requests.jsonl"
RATE_LOCK = RATE_DIR / "rate.lock"

TTL_SEC = 24 * 3600
WINDOW_SEC = 60
MAX_REQ = 95  # 100 cap minus 5-request safety margin (race avoidance)


def _ensure_dirs():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    RATE_DIR.mkdir(parents=True, exist_ok=True)


def cache_key(kind, query, params=None):
    """Hash query + canonical params. offset is intentionally NOT hashed
    (client-side rotation; per_page=20 covers all offsets on the same page)."""
    params = params or {}
    canonical = {
        "kind": kind,
        "query": query.strip(),
        "lang": params.get("lang", "zh"),
        "safesearch": params.get("safesearch", "true"),
        "image_type": params.get("image_type", ""),
        "orientation": params.get("orientation", ""),
        "per_page": params.get("per_page", 20),
        "page": params.get("page", 1),
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return f"{kind}_{hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]}.json"


def cache_get(key):
    """Return parsed JSON if cache file exists and mtime < TTL_SEC,
    else None (and remove stale file)."""
    _ensure_dirs()
    path = CACHE_DIR / key
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age >= TTL_SEC:
        try:
            path.unlink()
        except OSError:
            pass
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        try:
            path.unlink()
        except OSError:
            pass
        return None


def cache_set(key, payload):
    """Atomic JSON write. Caller is responsible for not caching error
    responses (caller passes the parsed 'hits' / 'videos' dict)."""
    _ensure_dirs()
    path = CACHE_DIR / key
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _load_timestamps():
    """Read requests.jsonl into a list of floats. Missing file → empty list."""
    if not RATE_LOG.exists():
        return []
    out = []
    try:
        with RATE_LOG.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(float(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def _write_timestamps(timestamps):
    """Rewrite requests.jsonl atomically (sort for diff-friendliness)."""
    tmp = RATE_LOG.with_suffix(RATE_LOG.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for t in timestamps:
            f.write(f"{t:.6f}\n")
    os.replace(tmp, RATE_LOG)


def rate_limit_acquire(sleep_fn=None):
    """Block until we have < MAX_REQ requests in the trailing WINDOW_SEC,
    then record a new timestamp under flock. Must be called once per outbound
    API request (NOT per cache hit).

    `sleep_fn` is injectable for tests; defaults to time.sleep.
    """
    if sleep_fn is None:
        sleep_fn = time.sleep
    _ensure_dirs()
    with RATE_LOCK.open("w") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            now = time.time()
            cutoff = now - WINDOW_SEC
            timestamps = _load_timestamps()
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= MAX_REQ:
                oldest = min(timestamps)
                wait = (oldest + WINDOW_SEC) - now + 0.05  # 50ms slack
                if wait > 0:
                    sleep_fn(wait)
                now = time.time()
                cutoff = now - WINDOW_SEC
                timestamps = [t for t in timestamps if t > cutoff]
            timestamps.append(now)
            _write_timestamps(timestamps)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def clear_for_tests():
    """Wipe cache + rate log (tests only)."""
    _ensure_dirs()
    for p in CACHE_DIR.glob("*.json"):
        try:
            p.unlink()
        except OSError:
            pass
    if RATE_LOG.exists():
        try:
            RATE_LOG.unlink()
        except OSError:
            pass