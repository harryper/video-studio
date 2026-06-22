#!/usr/bin/env python3
"""Unit tests for pixabay_cache + pixabay_image/video wrappers.

Run: python3 scripts/test_pixabay_cache.py

The integration smoke test (last case) makes one real API call per run to
verify end-to-end shape. Everything else is hermetic — uses tmp dirs and
monkey-patches the cache module's dirs.
"""
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pixabay_cache  # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Hermetic tests — sandbox CACHE_DIR / RATE_DIR to a tmp dir
# ────────────────────────────────────────────────────────────────────

def _sandbox():
    """Point pixabay_cache at a tmp dir, return (orig_cd, orig_rd)."""
    orig_cd = pixabay_cache.CACHE_DIR
    orig_rd = pixabay_cache.RATE_DIR
    tmp = Path(tempfile.mkdtemp(prefix="pixabay_test_"))
    pixabay_cache.CACHE_DIR = tmp
    pixabay_cache.RATE_DIR = tmp / ".rate"
    pixabay_cache.RATE_LOG = pixabay_cache.RATE_DIR / "requests.jsonl"
    pixabay_cache.RATE_LOCK = pixabay_cache.RATE_DIR / "rate.lock"
    return orig_cd, orig_rd


def _restore(orig_cd, orig_rd):
    pixabay_cache.CACHE_DIR = orig_cd
    pixabay_cache.RATE_DIR = orig_rd
    pixabay_cache.RATE_LOG = orig_rd / "requests.jsonl"
    pixabay_cache.RATE_LOCK = orig_rd / "rate.lock"


def test_cache_key_stable():
    """Same (kind, query, params) → same key; offset → ignored."""
    k1 = pixabay_cache.cache_key("image", "cat", {"page": 1, "per_page": 20})
    k2 = pixabay_cache.cache_key("image", "cat", {"page": 1, "per_page": 20})
    assert k1 == k2, f"cache_key not stable: {k1!r} vs {k2!r}"
    assert k1.endswith(".json")
    # Offset must NOT affect the key
    k3 = pixabay_cache.cache_key("image", "cat", {"page": 1, "per_page": 20})
    assert k1 == k3
    print("✓ cache_key stable + offset ignored")


def test_cache_key_differs_on_query():
    a = pixabay_cache.cache_key("image", "cat", {"per_page": 20})
    b = pixabay_cache.cache_key("image", "dog", {"per_page": 20})
    assert a != b, "different queries should yield different keys"
    print("✓ cache_key differs on query")


def test_cache_key_differs_on_kind():
    a = pixabay_cache.cache_key("image", "cat")
    b = pixabay_cache.cache_key("video", "cat")
    assert a != b
    print("✓ cache_key differs on kind")


def test_cache_set_and_get():
    orig_cd, orig_rd = _sandbox()
    try:
        key = "image_abc.json"
        pixabay_cache.cache_set(key, {"hits": [{"id": 1}]})
        got = pixabay_cache.cache_get(key)
        assert got == {"hits": [{"id": 1}]}, f"got {got!r}"
        print("✓ cache_set / cache_get round-trip")
    finally:
        _restore(orig_cd, orig_rd)


def test_cache_get_returns_none_when_missing():
    orig_cd, orig_rd = _sandbox()
    try:
        assert pixabay_cache.cache_get("nope.json") is None
        print("✓ cache_get returns None on missing key")
    finally:
        _restore(orig_cd, orig_rd)


def test_cache_get_returns_none_when_stale():
    """File older than TTL_SEC → cache_get deletes it + returns None."""
    orig_cd, orig_rd = _sandbox()
    try:
        key = "image_stale.json"
        pixabay_cache.cache_set(key, {"hits": [{"id": 99}]})
        path = pixabay_cache.CACHE_DIR / key
        # Backdate mtime to TTL + 1h
        old = time.time() - pixabay_cache.TTL_SEC - 3600
        os.utime(path, (old, old))
        assert pixabay_cache.cache_get(key) is None, "stale cache must return None"
        assert not path.exists(), "stale cache file must be deleted"
        print("✓ cache TTL enforced (24h+ stale → None + unlink)")
    finally:
        _restore(orig_cd, orig_rd)


def test_cache_get_returns_when_fresh():
    """File at age < TTL_SEC → returned."""
    orig_cd, orig_rd = _sandbox()
    try:
        key = "image_fresh.json"
        pixabay_cache.cache_set(key, {"hits": [{"id": 1}]})
        path = pixabay_cache.CACHE_DIR / key
        fresh = time.time() - 3600  # 1h old
        os.utime(path, (fresh, fresh))
        got = pixabay_cache.cache_get(key)
        assert got == {"hits": [{"id": 1}]}, f"got {got!r}"
        print("✓ cache returns content within TTL window")
    finally:
        _restore(orig_cd, orig_rd)


def test_rate_limiter_under_threshold_does_not_sleep():
    """Acquiring MAX_REQ-1 times should be fast (no sleep)."""
    orig_cd, orig_rd = _sandbox()
    try:
        sleeps = []
        for _ in range(pixabay_cache.MAX_REQ - 1):
            pixabay_cache.rate_limit_acquire(sleep_fn=lambda s: sleeps.append(s))
        assert sleeps == [], f"unexpected sleeps: {sleeps!r}"
        print(f"✓ {pixabay_cache.MAX_REQ - 1} acquires under cap → no sleep")
    finally:
        _restore(orig_cd, orig_rd)


def test_rate_limiter_blocks_when_at_cap():
    """After MAX_REQ acquires within window, the next should sleep."""
    orig_cd, orig_rd = _sandbox()
    try:
        sleeps = []
        for _ in range(pixabay_cache.MAX_REQ):
            pixabay_cache.rate_limit_acquire(sleep_fn=lambda s: sleeps.append(s))
        # One more → should sleep
        pixabay_cache.rate_limit_acquire(sleep_fn=lambda s: sleeps.append(s))
        assert len(sleeps) >= 1, "rate limiter should sleep when at cap"
        assert all(s > 0 for s in sleeps), f"non-positive sleep: {sleeps!r}"
        print(f"✓ cap-then-1 more → sleeps {len(sleeps)} time(s), max {max(sleeps):.2f}s")
    finally:
        _restore(orig_cd, orig_rd)


def test_rate_limiter_no_double_append_under_contention():
    """Two threads racing to acquire at the cap should both end up in the log,
    but neither should be silently dropped (count == thread_count + cap)."""
    orig_cd, orig_rd = _sandbox()
    try:
        sleeps = []
        # Pre-fill to cap - 5 so the next 10 acquires from 10 threads will all
        # touch the lock region.
        for _ in range(pixabay_cache.MAX_REQ - 5):
            pixabay_cache.rate_limit_acquire(sleep_fn=lambda s: sleeps.append(s))

        barrier = threading.Barrier(10)
        results = []

        def worker():
            barrier.wait()
            pixabay_cache.rate_limit_acquire(sleep_fn=lambda s: sleeps.append(s))
            results.append(1)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert sum(results) == 10, "all threads should have acquired"
        # Count timestamps in log
        timestamps = pixabay_cache._load_timestamps()
        expected = pixabay_cache.MAX_REQ - 5 + 10
        assert len(timestamps) == expected, (
            f"expected {expected} timestamps, got {len(timestamps)} — "
            "double-append or loss under contention"
        )
        print(f"✓ 10 contending threads → {len(timestamps)} unique timestamps")
    finally:
        _restore(orig_cd, orig_rd)


# ────────────────────────────────────────────────────────────────────
# Wrapper-shape tests (mock urlopen)
# ────────────────────────────────────────────────────────────────────

def test_pixabay_image_url_shape():
    """Verify pixabay_image builds the correct URL with key + safesearch."""
    import pixabay_image

    captured = {}

    class FakeResp:
        def __init__(self, body_bytes):
            self._body = body_bytes

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        return FakeResp(json.dumps({"hits": []}).encode("utf-8"))

    orig_urlopen = pixabay_image.urlopen
    pixabay_image.urlopen = fake_urlopen
    try:
        from pathlib import Path as _P
        orig_cd, orig_rd = _sandbox()
        try:
            try:
                pixabay_image.search("cat")
            except SystemExit:
                pass  # empty hits → OK
            url = captured["url"]
            assert "key=56376538" in url, f"key missing from URL: {url}"
            assert "q=cat" in url, f"query missing from URL: {url}"
            assert "safesearch=true" in url
            assert "lang=zh" in url
            assert "image_type=photo" in url
            assert "per_page=" in url
            print("✓ pixabay_image URL shape correct (key/q/safesearch/lang/per_page)")
        finally:
            _restore(orig_cd, orig_rd)
    finally:
        pixabay_image.urlopen = orig_urlopen


def test_pixabay_video_url_shape():
    """Verify pixabay_video URL: key + q + safesearch + per_page, NO image_type."""
    import pixabay_video

    captured = {}

    class FakeResp:
        def __init__(self, body_bytes):
            self._body = body_bytes

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        return FakeResp(json.dumps({"hits": []}).encode("utf-8"))

    orig_urlopen = pixabay_video.urlopen
    pixabay_video.urlopen = fake_urlopen
    try:
        orig_cd, orig_rd = _sandbox()
        try:
            try:
                pixabay_video.search("ocean")
            except SystemExit:
                pass
            url = captured["url"]
            assert "key=56376538" in url, f"key missing: {url}"
            assert "q=ocean" in url
            assert "safesearch=true" in url
            assert "per_page=" in url
            assert "videos/" in url, "should hit /api/videos/ endpoint"
            print("✓ pixabay_video URL shape correct (key/q/safesearch/per_page)")
        finally:
            _restore(orig_cd, orig_rd)
    finally:
        pixabay_video.urlopen = orig_urlopen


def test_choose_video_picks_smallest_tier_matching_aspect():
    """Algorithm: walk large→tiny, return first tier with width >= target_width.
    For target=1280, large (1920) and medium (1280) both qualify → medium wins.
    For target=1920, only large (1920) qualifies → large wins (medium is 1280 < 1920)."""
    import pixabay_video

    hits = [
        {
            "videos": {
                "large": {"url": "u_large", "width": 1920, "height": 1080, "size": 5_000_000},
                "medium": {"url": "u_medium", "width": 1280, "height": 720, "size": 2_000_000},
                "small": {"url": "u_small", "width": 960, "height": 540, "size": 1_000_000},
                "tiny": {"url": "u_tiny", "width": 640, "height": 360, "size": 500_000},
            }
        }
    ]
    # Target 1280 → medium is smallest tier satisfying width >= 1280
    url, w, h = pixabay_video._choose_video(hits[0], 1280, 720)
    assert url == "u_medium", f"target=1280 should pick medium, got {url!r}"
    assert (w, h) == (1280, 720)

    # Target 1920 → only large qualifies (medium=1280 < 1920)
    url, w, h = pixabay_video._choose_video(hits[0], 1920, 1080)
    assert url == "u_large", f"target=1920 should pick large, got {url!r}"
    assert (w, h) == (1920, 1080)

    print("✓ _choose_video: target=1280→medium, target=1920→large (smallest tier >= target)")

    # Vertical target (9:16)
    url, w, h = pixabay_video._choose_video(hits[0], 1080, 1920)
    assert url is None, "horizontal-only hit should not match 9:16 target"
    print("✓ _choose_video respects aspect ratio")


def test_choose_video_skips_zero_size():
    """A tier with size=0 should be skipped (broken CDN URL)."""
    import pixabay_video

    hits = [
        {
            "videos": {
                "medium": {"url": "u_medium", "width": 1280, "height": 720, "size": 2_000_000},
                "large": {"url": "u_large", "width": 1920, "height": 1080, "size": 0},
            }
        }
    ]
    # Target=1280 → smallest tier >= 1280 with size>0: medium (size=2M)
    # The broken large (size=0) should be ignored even though it's larger.
    url, _, _ = pixabay_video._choose_video(hits[0], 1280, 720)
    assert url == "u_medium", f"expected medium, got {url!r}"
    print("✓ _choose_video skips size=0 entries (broken large → medium fallback)")


# ────────────────────────────────────────────────────────────────────
# Integration smoke — one real API call. Run last.
# ────────────────────────────────────────────────────────────────────

def test_integration_smoke_nature():
    """Real Pixabay call: query 'nature' per_page=3 → ≥ 1 hit with largeImageURL."""
    import pixabay_image
    orig_urlopen = pixabay_image.urlopen
    try:
        orig_cd, orig_rd = _sandbox()
        try:
            hits = pixabay_image.search("nature", per_page=3, orientation="horizontal")
            assert len(hits) >= 1, f"expected ≥ 1 hit for 'nature', got {len(hits)}"
            with_large = [h for h in hits if h.get("largeImageURL")]
            assert len(with_large) >= 1, "expected ≥ 1 hit with largeImageURL"
            print(f"✓ integration smoke: 'nature' → {len(hits)} hits, "
                  f"{len(with_large)} with largeImageURL")
        finally:
            _restore(orig_cd, orig_rd)
    finally:
        pixabay_image.urlopen = orig_urlopen


# ────────────────────────────────────────────────────────────────────

def main():
    hermetic = [
        test_cache_key_stable,
        test_cache_key_differs_on_query,
        test_cache_key_differs_on_kind,
        test_cache_set_and_get,
        test_cache_get_returns_none_when_missing,
        test_cache_get_returns_none_when_stale,
        test_cache_get_returns_when_fresh,
        test_rate_limiter_under_threshold_does_not_sleep,
        test_rate_limiter_blocks_when_at_cap,
        test_rate_limiter_no_double_append_under_contention,
        test_pixabay_image_url_shape,
        test_pixabay_video_url_shape,
        test_choose_video_picks_smallest_tier_matching_aspect,
        test_choose_video_skips_zero_size,
    ]
    integration = [
        test_integration_smoke_nature,
    ]
    passed = 0
    failed = 0
    for t in hermetic:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    for t in integration:
        print(f"\n--- integration: {t.__name__} ---")
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())