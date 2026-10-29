import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.api.v1.api import api_router as api_router_v1
from app.core.config import settings
from app.core.security import is_valid_service_token


class CVUploadGuard:
    """Autentica antes del parseo multipart y acota el cuerpo ASGI completo."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or not scope.get("path", "").endswith("/cv-analyses"):
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin1")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""

        async def reject(status_code: int, detail: str) -> None:
            body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": status_code, "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})

        if not is_valid_service_token(token):
            await reject(401, "Credenciales de servicio inválidas.")
            return

        max_body = settings.CV_MAX_PDF_BYTES + settings.CV_MAX_UPLOAD_BODY_OVERHEAD
        declared_length = headers.get(b"content-length")
        if declared_length:
            try:
                if int(declared_length) > max_body:
                    await reject(413, f"El archivo supera el máximo permitido de {settings.CV_MAX_PDF_BYTES // (1024 * 1024)} MiB.")
                    return
            except ValueError:
                await reject(400, "El tamaño de la solicitud no es válido.")
                return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > max_body:
                await reject(413, f"El archivo supera el máximo permitido de {settings.CV_MAX_PDF_BYTES // (1024 * 1024)} MiB.")
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


# Esta es la aplicacion ASGI que Uvicorn sirve. FastAPI usa las anotaciones de
# tipo y los modelos Pydantic de las rutas para validar JSON y generar /docs.
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.API_VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
)
app.add_middleware(CVUploadGuard)


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # No devolver valores de entrada ni los mensajes técnicos en inglés de Pydantic.
    return JSONResponse(status_code=422, content={"detail": "La solicitud contiene datos inválidos o incompletos."})

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

    return {"message": "La API de análisis de CV está disponible."}


# Agrupar las rutas en routers mantiene main.py pequeno. El prefijo final sera
# /api/v1 porque settings.API_V1_STR se aplica al incluir este router.
app.include_router(api_router_v1, prefix=settings.API_V1_STR)
