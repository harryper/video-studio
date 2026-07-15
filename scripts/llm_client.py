#!/usr/bin/env python3
"""Thin Claude Messages-API client for the video-studio daemons.

Replaces the old `openclaw agent` subprocess LLM backend. Connection info is
read from the project-local `llm_config.json` (the `env` block) so daemons
don't depend on systemd EnvironmentFile= or the Claude Code client's own
settings — the latter may point at a gateway that rejects bare SDK calls.
An env var of the same name overrides the file, and the config path itself
is overridable via LLM_CONFIG_FILE.

`llm_config.json` carries the real token and is gitignored; see
`llm_config.json.example` for the expected shape.

The header set sent with each request is configurable (LLM_CLIENT_HEADERS,
JSON) in case an endpoint expects a specific fingerprint. Default is empty.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

import anthropic

_SETTINGS_FILE = Path(
    os.environ.get("LLM_CONFIG_FILE")
    or Path(__file__).resolve().parents[1] / "llm_config.json"
)
_DEFAULT_MODEL = "claude-opus-4-8"


def _settings_env() -> dict:
    """The `env` block from llm_config.json, or {} if unreadable."""
    try:
        return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8")).get("env", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _cfg(name: str, default: str = "") -> str:
    """Resolve a setting: real env var wins, then llm_config.json, then default."""
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
    # llm_config.json actually carries.
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
