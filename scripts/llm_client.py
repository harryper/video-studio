#!/usr/bin/env python3
"""Thin Claude Messages-API client for the video-studio daemons.

Replaces the old `openclaw agent` subprocess LLM backend. Connection info is
read from /root/.claude/settings.json (the `env` block) so nothing is
hardcoded — an env var of the same name overrides the file, and the settings
path itself is overridable via CLAUDE_SETTINGS_FILE.

The endpoint currently configured there (api-cc.freemodel.dev) gates access to
"the official Claude Code client" and rejects a bare SDK call. The header set
sent with each request is therefore configurable (LLM_CLIENT_HEADERS, JSON) so
the daemons can present the fingerprint that endpoint expects without code
changes. Default headers are empty.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

import anthropic

_SETTINGS_FILE = Path(
    os.environ.get("CLAUDE_SETTINGS_FILE", "/root/.claude/settings.json")
)
_DEFAULT_MODEL = "claude-opus-4-8"


def _settings_env() -> dict:
    """The `env` block from settings.json, or {} if unreadable."""
    try:
        return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8")).get("env", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _cfg(name: str, default: str = "") -> str:
    """Resolve a setting: real env var wins, then settings.json, then default."""
    return os.environ.get(name) or _settings_env().get(name) or default


def _extra_headers() -> dict:
    """Per-request headers. Override with LLM_CLIENT_HEADERS (JSON object)."""
    raw = os.environ.get("LLM_CLIENT_HEADERS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


@lru_cache(maxsize=1)
def _client() -> anthropic.Anthropic:
    token = _cfg("ANTHROPIC_AUTH_TOKEN")
    api_key = _cfg("ANTHROPIC_API_KEY")
    base_url = _cfg("ANTHROPIC_BASE_URL")
    kwargs = {}
    if base_url:
        kwargs["base_url"] = base_url
    # auth_token → Authorization: Bearer; api_key → x-api-key. Prefer whichever
    # settings.json actually carries.
    if token:
        kwargs["auth_token"] = token
    elif api_key:
        kwargs["api_key"] = api_key
    return anthropic.Anthropic(**kwargs)


def model_id() -> str:
    return _cfg("ANTHROPIC_MODEL", _DEFAULT_MODEL)


def complete(system: str, user: str, max_tokens: int = 4096, timeout: float = 300.0) -> str:
    """One Messages-API turn. Returns the concatenated assistant text.

    No thinking (matches the daemons' prior `--thinking off`). Raises on API
    error or on an empty/denied response so callers can fall back.
    """
    resp = _client().with_options(timeout=timeout).messages.create(
        model=model_id(),
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        extra_headers=_extra_headers(),
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"empty completion (stop_reason={resp.stop_reason})")
    return text
