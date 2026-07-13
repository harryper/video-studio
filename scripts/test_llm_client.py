#!/usr/bin/env python3
"""Offline tests for scripts/llm_client.py.

No live HTTP, no real API key. Stubs `anthropic.Anthropic` at import time
so the module is loaded without touching the network. Validates:
  - settings.json env resolution + env-var override precedence
  - client construction picks auth_token vs api_key correctly
  - extra_headers parsing of LLM_CLIENT_HEADERS (valid / invalid / missing)
  - complete() concatenates text blocks and raises on empty responses

Run: python3 scripts/test_llm_client.py
"""
import json
import sys
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

# Build a stub anthropic module BEFORE importing llm_client so the latter
# can do `import anthropic` without requiring network or a real key.
sys.modules.setdefault("anthropic", MagicMock())

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_client  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────

def _write_settings(env: dict) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"env": env}, f)
    f.close()
    return Path(f.name)


def _fresh_client(monkey_settings: Optional[Path] = None):
    """Re-import llm_client with a fresh _client cache + custom settings."""
    llm_client._client.cache_clear()
    if monkey_settings is not None:
        llm_client._SETTINGS_FILE = monkey_settings


# ── tests ────────────────────────────────────────────────────────────

def test_settings_env_parsing():
    """Read env block from settings.json; tolerate missing file."""
    settings = _write_settings({
        "ANTHROPIC_AUTH_TOKEN": "fe_oa_test",
        "ANTHROPIC_BASE_URL": "https://example.test",
        "ANTHROPIC_MODEL": "claude-opus-4-8",
    })
    _fresh_client(settings)
    with patch.dict("os.environ", {}, clear=True):
        assert llm_client._settings_env()["ANTHROPIC_BASE_URL"] == "https://example.test"
        assert llm_client._cfg("ANTHROPIC_BASE_URL") == "https://example.test"
        assert llm_client.model_id() == "claude-opus-4-8"
    print("✓ settings.json env block resolves correctly")


def test_env_var_overrides_settings():
    """Real env vars beat settings.json."""
    settings = _write_settings({"ANTHROPIC_MODEL": "claude-opus-4-8"})
    _fresh_client(settings)
    with patch.dict("os.environ", {"ANTHROPIC_MODEL": "claude-sonnet-5"}):
        assert llm_client.model_id() == "claude-sonnet-5", "env should win"
    print("✓ env var overrides settings.json")


def test_missing_settings_file_is_safe():
    """Pointing at a missing settings file yields empty env, no crash."""
    _fresh_client(Path("/nonexistent/path/settings.json"))
    with patch.dict("os.environ", {}, clear=True):
        env = llm_client._settings_env()
        assert env == {}, f"missing settings should yield empty env, got {env}"
        # And the model falls back to default
        assert llm_client.model_id() == "claude-opus-4-8"
    print("✓ missing settings.json falls back cleanly")


def test_client_picks_auth_token():
    """When ANTHROPIC_AUTH_TOKEN is set, pass auth_token (Bearer), not api_key."""
    settings = _write_settings({
        "ANTHROPIC_AUTH_TOKEN": "fe_oa_test",
        "ANTHROPIC_BASE_URL": "https://example.test",
    })
    _fresh_client(settings)
    captured = {}
    fake_cls = MagicMock()
    def _capture(**kw):
        captured.update(kw)
        return MagicMock()
    fake_cls.side_effect = _capture
    with patch.dict("os.environ", {}, clear=True):
        with patch.object(llm_client, "anthropic") as mod:
            mod.Anthropic = fake_cls
            llm_client._client.cache_clear()
            llm_client._client()
    assert captured.get("auth_token") == "fe_oa_test", f"expected auth_token, got {captured}"
    assert "api_key" not in captured, "should not set api_key when auth_token present"
    assert captured.get("base_url") == "https://example.test"
    print("✓ client uses auth_token + base_url when both are in settings")


def test_client_picks_api_key_when_no_token():
    """Without ANTHROPIC_AUTH_TOKEN, fall back to ANTHROPIC_API_KEY."""
    settings = _write_settings({
        "ANTHROPIC_API_KEY": "sk-test",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
    })
    _fresh_client(settings)
    captured = {}
    fake_cls = MagicMock(side_effect=lambda **kw: (captured.update(kw) or MagicMock()))
    with patch.dict("os.environ", {}, clear=True):
        with patch.object(llm_client, "anthropic") as mod:
            mod.Anthropic = fake_cls
            llm_client._client.cache_clear()
            llm_client._client()
    assert captured.get("api_key") == "sk-test"
    assert "auth_token" not in captured
    print("✓ client uses api_key when auth_token absent")


def test_extra_headers_parsing():
    """LLM_CLIENT_HEADERS=JSON parses; bad JSON / non-dict yields {}."""
    with patch.dict("os.environ", {"LLM_CLIENT_HEADERS": '{"x-foo": "bar"}'}):
        assert llm_client._extra_headers() == {"x-foo": "bar"}
    with patch.dict("os.environ", {"LLM_CLIENT_HEADERS": "not json"}):
        assert llm_client._extra_headers() == {}
    with patch.dict("os.environ", {"LLM_CLIENT_HEADERS": "[]"}):
        # non-dict JSON → empty (rejected)
        assert llm_client._extra_headers() == {}
    with patch.dict("os.environ", {}, clear=True):
        assert llm_client._extra_headers() == {}
    print("✓ LLM_CLIENT_HEADERS parsing (valid / invalid / missing)")


def test_complete_concatenates_text_blocks():
    """complete() joins multiple text content blocks into one string."""
    fake_resp = MagicMock()
    fake_resp.content = [
        MagicMock(type="text", text="hello "),
        MagicMock(type="text", text="world"),
    ]
    fake_resp.stop_reason = "end_turn"
    fake_client = MagicMock()
    fake_client.with_options.return_value.messages.create.return_value = fake_resp
    with patch.object(llm_client, "_client", return_value=fake_client):
        out = llm_client.complete(system="S", user="U")
    assert out == "hello world", f"got {out!r}"
    print("✓ complete() concatenates text blocks")


def test_complete_raises_on_empty():
    """If no text blocks, raise — caller falls back."""
    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(type="text", text="")]
    fake_resp.stop_reason = "end_turn"
    fake_client = MagicMock()
    fake_client.with_options.return_value.messages.create.return_value = fake_resp
    with patch.object(llm_client, "_client", return_value=fake_client):
        try:
            llm_client.complete(system="S", user="U")
        except RuntimeError as e:
            assert "empty completion" in str(e)
            print("✓ complete() raises on empty response")
            return
    raise AssertionError("expected RuntimeError on empty response")


def main():
    tests = [
        test_settings_env_parsing,
        test_env_var_overrides_settings,
        test_missing_settings_file_is_safe,
        test_client_picks_auth_token,
        test_client_picks_api_key_when_no_token,
        test_extra_headers_parsing,
        test_complete_concatenates_text_blocks,
        test_complete_raises_on_empty,
    ]
    passed = 0
    failed = 0
    for t in tests:
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
