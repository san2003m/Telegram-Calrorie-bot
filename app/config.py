from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    owner_telegram_id: int = 0
    public_signup: bool = False

    openai_api_key: str = ""
    openai_model: str = "gpt-5.6-terra"

    database_url: str = "sqlite+aiosqlite:///./data/calorie_bot.db"
    app_timezone: str = "Asia/Seoul"
    data_dir: Path = Path("data")

    log_level: str = "INFO"
    log_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    log_backup_count: int = Field(default=10, ge=1, le=100)

    health_host: str = "127.0.0.1"
    health_port: int = Field(default=8080, ge=1, le=65535)
    openfoodfacts_user_agent: str = (
        "calorie-telegram-bot/0.1 (personal-use; contact=local@example.invalid)"
    )

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "calorie-bot.log"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.chmod(0o700)


@lru_cache
def get_settings() -> Settings:
    return Settings()
