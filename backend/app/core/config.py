from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import AnyHttpUrl
from enum import Enum


ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class ModeEnum(str, Enum):
    development = "development"
    production = "production"
    testing = "testing"


class Settings(BaseSettings, extra='ignore'):
    PROJECT_NAME: str = "app"
    BACKEND_CORS_ORIGINS: list[str] | list[AnyHttpUrl]
    MODE: ModeEnum = ModeEnum.development
    API_VERSION: str = "v1"
    API_V1_STR: str = f"/api/{API_VERSION}"
    GROQ_API_KEY: str
    JWT_SECRET_KEY: str
    SERVICE_USER: str
    SERVICE_PASSWORD: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    CV_MODEL: str = "qwen/qwen3-32b"
    CV_BLOQUE_CARACTERES: int = 4000
    GROQ_MAX_TOKENS: int = 950
    GROQ_PAUSA_SEGUNDOS: int = 12
    
    class Config:
        case_sensitive = True
        env_file = ENV_FILE


settings = Settings()
