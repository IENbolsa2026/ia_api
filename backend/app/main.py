from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.api.v1.api import api_router as api_router_v1
from app.core.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Hook de ciclo de vida de FastAPI.

    FastAPI ejecuta el codigo anterior a ``yield`` al iniciar y el posterior al
    detenerse. Es el lugar indicado para abrir/cerrar recursos compartidos si
    mas adelante se agregan conexiones propias de la API web.
    """

    yield

# Esta es la aplicacion ASGI que Uvicorn sirve. FastAPI usa las anotaciones de
# tipo y los modelos Pydantic de las rutas para validar JSON y generar /docs.
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.API_VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# CORS solo afecta a navegadores. La llamada servidor-a-servidor del callback no
# necesita CORS, pero el frontend que consuma la API posiblemente si.
if settings.BACKEND_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

@app.get("/")
async def root() -> dict[str, str]:
    """Endpoint simple para comprobar que el servidor HTTP esta disponible."""

    return {"message": "CV analyzer API is running"}


# Agrupar las rutas en routers mantiene main.py pequeno. El prefijo final sera
# /api/v1 porque settings.API_V1_STR se aplica al incluir este router.
app.include_router(api_router_v1, prefix=settings.API_V1_STR)
