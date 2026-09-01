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
    openai_recipe_model: str = "gpt-5.6-luna"
    openai_menu_model: str = "gpt-5.6-luna"
    recipe_ai_daily_limit: int = Field(default=10, ge=0, le=10_000)
    recipe_ai_monthly_limit: int = Field(default=100, ge=0, le=100_000)
    recipe_ai_max_input_chars: int = Field(default=2_000, ge=200, le=20_000)
    recipe_ai_max_output_tokens: int = Field(default=800, ge=128, le=4_096)
    recipe_max_ingredients: int = Field(default=20, ge=1, le=50)
    recipe_ai_cooldown_seconds: float = Field(default=10.0, ge=0, le=3_600)
    menu_ai_daily_limit: int = Field(default=5, ge=0, le=10_000)
    menu_ai_monthly_limit: int = Field(default=50, ge=0, le=100_000)
    menu_ai_global_daily_limit: int = Field(default=20, ge=0, le=100_000)
    menu_ai_global_monthly_limit: int = Field(default=200, ge=0, le=1_000_000)
    menu_ai_max_query_chars: int = Field(default=80, ge=10, le=160)
    menu_ai_max_output_tokens: int = Field(default=900, ge=128, le=4_096)
    menu_ai_cooldown_seconds: float = Field(default=15.0, ge=0, le=3_600)
    menu_search_cache_days: int = Field(default=7, ge=1, le=365)

    mfds_api_key: str = ""
    mfds_api_timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)

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
