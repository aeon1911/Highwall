from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HIGH_", env_file=BASE_DIR / ".env", extra="ignore")

    secret_key: str = "dev-insecure-change-me"
    site_name: str = "High"
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'high.db'}"
    debug: bool = False

    storage_backend: str = "telegram"  # telegram | local
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    max_upload_mb: int = 20

    min_long_side: int = 2560
    min_short_side: int = 1440

    cache_dir: Path = BASE_DIR / "data" / "cache"
    local_storage_dir: Path = BASE_DIR / "data" / "files"
    cache_max_mb: int = 2048

    per_page: int = 24
    thumb_width: int = 480
    session_max_age: int = 60 * 60 * 24 * 30  # 30 dias
    min_age: int = 18

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
