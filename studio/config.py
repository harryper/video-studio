from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDIO_", extra="ignore")

    database_url: str = "sqlite+pysqlite:///./studio.db"
    artifact_dir: Path = Path("media")
    provider_name: str = "stub"
    search_provider_name: str = "stub"
    lease_seconds: int = 300
    search_provider_url: str = ""
    search_provider_token: str = ""
