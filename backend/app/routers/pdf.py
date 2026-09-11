import asyncio
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.config import settings
from app.core.connection_manager import manager
from app.schemas.analisis import EstadoTarea, ProcesarPdfResponse
from app.tasks.procesar_pdf import procesar_pdf as tarea_procesar_pdf

router = APIRouter(tags=["pdf"])


@router.post(
    "/procesar-pdf",
    response_model=ProcesarPdfResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Sube un PDF y dispara el análisis asincrónico",
)
async def procesar_pdf(
    file: Annotated[UploadFile, File(description="Archivo PDF a analizar")],
    job_id: Annotated[
        str | None,
        Form(description="ID de tarea opcional para reconectar el websocket /ws/{job_id}."),
    ] = None,
) -> ProcesarPdfResponse:
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo debe ser un PDF (content-type application/pdf).",
        )

    contenido = await file.read()
    if not contenido:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El archivo está vacío.")

    max_bytes = settings.PDF_MAX_MB * 1024 * 1024
    if len(contenido) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"El PDF supera el límite de {settings.PDF_MAX_MB} MB.",
        )

    id_tarea = job_id or str(uuid4())
    nombre = file.filename or "documento.pdf"
    print(f"\n[FLUJO] POST /procesar-pdf recibe '{nombre}' de {len(contenido)} bytes · job_id={id_tarea}")
    manager.crear_job(id_tarea, nombre)
    print(f"[FLUJO] job {id_tarea} registrado en memoria (estado PENDIENTE).")

    asyncio.get_running_loop().create_task(
        tarea_procesar_pdf(id_tarea, nombre, contenido)
    )
    print("[FLUJO] create_task lanzó la tarea en background → respondo 202 al cliente")

    return ProcesarPdfResponse(job_id=id_tarea, estado=EstadoTarea.procesando.value)