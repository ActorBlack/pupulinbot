from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PUPULIN_", env_file=".env", extra="ignore")

    database: Path = Path("data/pupulin.db")
    paipuya_base_url: str = "https://5-data.amae-koromo.com/api/v2/pl4"
    paipuya_sanma_base_url: str = "https://3-data.amae-koromo.com/api/v2/pl3"
    paipuya_timeout: float = 15
    monitor_interval: int = Field(default=60, ge=20)
    monitor_page_size: int = Field(default=20, ge=1, le=200)
    mortal_api_url: str | None = None
    mortal_api_token: str | None = None
    mortal_timeout: float = 180
    review_workers: int = Field(default=1, ge=1, le=4)
    live_api_url: str | None = None
    live_api_token: str | None = None
    admin_users: set[str] = Field(default_factory=set)


settings = Settings()
