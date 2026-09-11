from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    PROJECT_NAME: str = "pdf_analysis_service"
    BACKEND_CORS_ORIGINS: list[str] = ["*"]
    LOG_LEVEL: str = "INFO"

    # Groq
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    GROQ_MAX_TOKENS: int = 4096
    GROQ_TIMEOUT: float = 120.0

    # Django webhook
    DJANGO_WEBHOOK_URL: str = ""

    # PDF
    PDF_MAX_MB: int = 20
    PDF_MAX_CHARACTERES: int = 40_000


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()