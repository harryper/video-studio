from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDIO_", extra="ignore")

    database_url: str = "sqlite+pysqlite:///./studio.db"
    artifact_dir: Path = Path("media")
    provider_name: str = "stub"
    search_provider_name: str = "stub"
    lease_seconds: int = 300
    # STUDIO_SEARCH_PROVIDER_URL — base URL for the HTTP search adapter.
    # Empty value disables ``HttpSearchProvider`` (raises ModelProviderError).
    search_provider_url: str = ""
    # STUDIO_SEARCH_PROVIDER_TOKEN — optional bearer token for the search API.
    search_provider_token: str = ""

    # Authentication: single-user session via STUDIO_CONTENT_STUDIO_PASSWORD.
    content_studio_password: str = ""
    # STUDIO_CONTENT_STUDIO_SESSION_SECRET — optional HMAC key. When empty, a
    # random 32-byte hex is generated at startup (process-local); the resulting
    # cookies are unforgeable outside that process.
    content_studio_session_secret: str = ""
    # STUDIO_PRODUCTION — when True, session cookies are marked ``Secure``.
    production: bool = False

    # STUDIO_ANTHROPIC_API_KEY — required for live Anthropic calls. The worker
    # fail-fasts at startup when this is empty so a misconfigured deploy shows
    # the error in the systemd journal rather than burning the lease window
    # on every queued job.
    anthropic_api_key: str = ""

    # SSE handler tunables (test-only overrides shorten poll + heartbeat).
    sse_poll_interval_ms: int = 500
    sse_heartbeat_ms: int = 15_000
    # ``sse_max_runtime_ms`` caps how long a single SSE connection lives.
    # Production uses the default (open-ended); tests shorten it so the
    # response body can be collected and assertions can run in <5 s.
    sse_max_runtime_ms: int = 0
