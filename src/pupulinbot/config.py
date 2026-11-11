from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PUPULIN_", env_file=".env", extra="ignore")

    database: Path = Path("data/pupulin.db")
    legacy_follow_group: str | None = None
    paipuya_base_url: str = "https://5-data.amae-koromo.com/api/v2/pl4"
    paipuya_sanma_base_url: str = "https://5-data.amae-koromo.com/api/v2/pl3"
    paipuya_timeout: float = Field(default=60, ge=5, le=180)
    paipuya_api_token: str | None = Field(default=None, repr=False)
    paipuya_rate_limit_file: Path = Path("data/paipuya-rate.sqlite")
    paipuya_auto_cap: bool = False
    cap_endpoint: str = "https://akcap.pikapika.me/14f343ec68/"
    cap_cache_dir: Path = Path("data/cap")
    cap_workers: int = Field(default=2, ge=1, le=8)
    cap_timeout: float = Field(default=60, gt=0, le=180)
    hashwx_library: str | None = None
    monitor_interval: int = Field(default=60, ge=20)
    monitor_notify_start: bool = True
    monitor_owner: str | None = None
    monitor_only_groups: set[str] = Field(default_factory=set)
    monitor_catchup_limit: int = Field(default=200, ge=20, le=2000)
    monitor_page_size: int = Field(default=20, ge=1, le=200)
    mortal_api_url: str | None = None
    mortal_online: bool = False
    mortal_browser_profile: Path = Path("data/mortal-chrome")
    mortal_online_model: str = "4.1b"
    mortal_online_cooldown: float = Field(default=10, ge=10, le=300)
    mortal_online_timeout: float = Field(default=600, ge=30, le=3600)
    mortal_online_headless: bool = True
    mortal_online_retries: int = Field(default=2, ge=0, le=5)
    mortal_online_proxy: str | None = None
    mortal_save_html: bool = True
    mortal_save_screenshot: bool = False
    mortal_api_token: str | None = None
    mortal_timeout: float = Field(default=180, gt=0)
    review_workers: int = Field(default=1, ge=1, le=4)
    review_queue_size: int = Field(default=20, ge=1, le=1000)
    batch_directory: Path = Path("data/batches")
    spectator_browser: bool = False
    spectator_browser_profile: Path = Path("data/majsoul-spectator")
    live_api_url: str | None = None
    live_api_token: str | None = None
    admin_users: set[str] = Field(default_factory=set)


settings = Settings()
