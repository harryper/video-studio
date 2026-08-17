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
