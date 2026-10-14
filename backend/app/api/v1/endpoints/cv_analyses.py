import base64

from celery.result import AsyncResult
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from app.core.celery_app import celery_app
from app.schemas.cv_analysis import (
    CVAnalysisAccepted,
    CVAnalysisStatus,
    CVAnalysisTaskStatus,
)
from app.tasks.cv_analysis_tasks import analyze_cv_task


router = APIRouter()


@router.post(
    "/cv-analyses",
    response_model=CVAnalysisAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_cv_analysis(
    analysis_id: int = Form(gt=0),
    candidate_id: int = Form(gt=0),
    user_id: int | None = Form(default=None, gt=0),
    cv: UploadFile = File(...),
) -> CVAnalysisAccepted:
    """Acepta un PDF y envia su extracción y análisis al worker de Celery.

    Una ruta ``async`` de FastAPI debe terminar rapido: su funcion es recibir,
    validar y responder HTTP. El PDF se serializa para Redis; su lectura y el
    análisis con Groq suceden en el worker, fuera de la petición HTTP.
    """

    if cv.content_type not in {"application/pdf", "application/x-pdf"}:
        raise HTTPException(status_code=415, detail="El CV debe enviarse en formato PDF.")

    pdf_bytes = await cv.read()
    if not pdf_bytes:
        raise HTTPException(status_code=422, detail="El PDF está vacío.")
    if len(pdf_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El PDF supera el máximo de 5 MB.")

    task = analyze_cv_task.delay(
        analysis_id=analysis_id,
        candidate_id=candidate_id,
        user_id=user_id,
        cv_pdf_base64=base64.b64encode(pdf_bytes).decode("ascii"),
    )

    # 202 comunica correctamente que el servidor acepto el pedido, pero aun no
    # tiene el resultado. task.id sirve para consultar el endpoint GET.
    return CVAnalysisAccepted(
        job_id=task.id,
        status=CVAnalysisStatus.queued,
    )


@router.get(
    "/cv-analyses/{job_id}",
    response_model=CVAnalysisTaskStatus,
)
async def retrieve_cv_analysis(
    job_id: str,
) -> CVAnalysisTaskStatus:
    """Traduce los estados de Celery al contrato publico de esta API."""

    task = AsyncResult(job_id, app=celery_app)
    celery_statuses = {
        "PENDING": CVAnalysisStatus.queued,
        "RECEIVED": CVAnalysisStatus.queued,
        "STARTED": CVAnalysisStatus.processing,
        "RETRY": CVAnalysisStatus.processing,
        "SUCCESS": CVAnalysisStatus.completed,
        "FAILURE": CVAnalysisStatus.failed,
        "REVOKED": CVAnalysisStatus.failed,
    }
    api_status = celery_statuses.get(task.state, CVAnalysisStatus.processing)

    # El worker devuelve un dict al completar. Si falla, Celery guarda la
    # excepcion como resultado; se transforma a texto para el cliente HTTP.
    result = task.result if task.successful() and isinstance(task.result, dict) else None
    error = str(task.result) if task.failed() else None

    return CVAnalysisTaskStatus(
        job_id=job_id,
        status=api_status,
        result=result,
        error=error,
    )
