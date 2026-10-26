from pydantic_settings import BaseSettings
from pydantic import model_validator
from typing import Optional


class Settings(BaseSettings):
    # Required at runtime — set BOT_TOKEN in your host environment or .env
    BOT_TOKEN: str = ""
    DATABASE_URL: str = "sqlite+aiosqlite:///./alexa.db"

    # Gemini key is loaded from private runtime config; never commit a real key.
    GEMINI_API_KEY: str = ""
    API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"

    # Ollama-compatible AI provider settings; keys remain runtime-only.
    OLLAMA_API_KEY: str = ""
    OLLAMA_BASE_URL: str = "https://ollama.com"
    OLLAMA_MODEL: str = "qwen3:235b-cloud"

    WEBHOOK_URL: str = ""
    WEBHOOK_SECRET: str = "guardian_x_secret"
    WEBHOOK_PORT: int = 8080
    WEBHOOK_PATH: str = "/webhook"

    REDIS_URL: str = "redis://localhost:6379/0"

    BOT_OWNER_ID: int = 0
    # Friendly aliases accepted by ALEXA deployment configs.
    OWNER_ID: int = 0
    API_ID: int = 0
    API_HASH: str = ""
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "production"

    SENTRY_DSN: Optional[str] = None
    MAX_CONNECTIONS: int = 10
    POOL_SIZE: int = 5

    DAILY_REWARD: int = 100
    WEEKLY_REWARD: int = 500
    MONTHLY_REWARD: int = 2000
    REFERRAL_REWARD: int = 150
    PROTECT_COST_PER_DAY: int = 500
    REVIVE_COST: int = 250
    MAX_ROB_AMOUNT: int = 500
    ACTIVITY_XP_PER_MSG: int = 2
    MAX_WARN_COUNT: int = 3
    FLOOD_LIMIT: int = 5
    FLOOD_WINDOW: int = 10

    SUPPORTED_LANGUAGES: list[str] = ["en"]
    DEFAULT_LANGUAGE: str = "en"

    @model_validator(mode="after")
    def fix_database_url(self) -> "Settings":
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            url = "postgresql+asyncpg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+asyncpg://" + url[len("postgresql://"):]
        self.DATABASE_URL = url
        return self

    @property
    def effective_owner_id(self) -> int:
        return self.BOT_OWNER_ID or self.OWNER_ID

    @property
    def is_configured(self) -> bool:
        """True only when all required runtime secrets are provided."""
        return bool(self.BOT_TOKEN)

    class Config:
        env_file = ".env"
        case_sensitive = True


settings = Settings()


def get_gemini_api_key() -> str:
    """Get Gemini key from private runtime settings or an ignored local key file."""
    import os

    key = (settings.GEMINI_API_KEY or settings.API_KEY or
           os.getenv("GEMINI_API_KEY", "") or os.getenv("API_KEY", "")).strip()
    if key:
        return key
    try:
        from bot.gemini_secret import GEMINI_API_KEY
        return str(GEMINI_API_KEY).strip()
    except (ImportError, AttributeError, TypeError):
        return ""
