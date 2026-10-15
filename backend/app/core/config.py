from enum import Enum
from pathlib import Path

from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings


ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class ModeEnum(str, Enum):
    development = "development"
    production = "production"
    testing = "testing"


class Settings(BaseSettings, extra="ignore"):
    """Configuracion centralizada leida desde ``backend/.env``.

    Pydantic Settings convierte las variables de entorno en atributos Python con
    tipo. Por ejemplo, ``CV_BLOQUE_CARACTERES=5000`` pasa a ser un ``int``.
    Mantener la configuracion aqui evita leer ``os.environ`` repartido por toda
    la aplicacion y hace visibles los valores necesarios para arrancar.
    """

    PROJECT_NAME: str = "app"
    BACKEND_CORS_ORIGINS: list[str] | list[AnyHttpUrl] = []
    MODE: ModeEnum = ModeEnum.development
    API_VERSION: str = "v1"
    API_V1_STR: str = f"/api/{API_VERSION}"
    LLM_PROVIDER: str = "groq"
    GROQ_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_MAX_OUTPUT_TOKENS: int = 950
    OPENAI_REQUESTS_PER_MINUTE: int = 20
    JWT_SECRET_KEY: str
    SERVICE_USER: str
    SERVICE_PASSWORD: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    CV_MODEL: str = "qwen/qwen3.8-27b"
    CV_BLOQUE_CARACTERES: int = 4000
    GROQ_MAX_TOKENS: int = 950
    GROQ_PAUSA_SEGUNDOS: int = 12
    GROQ_REQUESTS_PER_MINUTE: int = 20
    CV_MAX_PDF_BYTES: int = 5 * 1024 * 1024
    SERVICE_API_TOKEN: str = ""
    MATCH_GROQ_MODEL: str = ""
    MATCH_REDIS_URL: str = "redis://localhost:6379/4"
    JOB_STORE_REDIS_URL: str = "redis://localhost:6379/2"
    JOB_TTL_SECONDS: int = 60 * 60 * 24 * 30

    # Redis recibe las tareas. El backend guarda estado y resultado para que el
    # endpoint GET pueda informar queued/processing/completed/failed.
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    # Compartida con CHANNEL_REDIS_URL de Django para los eventos WebSocket.
    CHANNEL_REDIS_URL: str = "redis://localhost:6379/3"

    # URL de la otra aplicacion. Al terminar el analisis se hace POST aqui.
    CV_RESULT_CALLBACK_URL: AnyHttpUrl
    # Se envia en un header solamente si se configura; sirve para autenticar el
    # callback entre ambos servicios sin exponer la clave de Groq.
    CV_RESULT_CALLBACK_SECRET: str | None = None
    CV_RESULT_CALLBACK_HMAC_SECRET: str | None = None

    class Config:
        case_sensitive = True
        env_file = ENV_FILE


settings = Settings()
