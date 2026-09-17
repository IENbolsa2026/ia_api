from celery.result import AsyncResult
from fastapi import APIRouter, status

from app.core.celery_app import celery_app
from app.schemas.cv_analysis import (
    CVAnalysisAccepted,
    CVAnalysisRequest,
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
    payload: CVAnalysisRequest,
) -> CVAnalysisAccepted:
    """Acepta un CV y envia el trabajo al broker de Celery.

    Una ruta ``async`` de FastAPI debe terminar rapido: su funcion es recibir,
    validar y responder HTTP. ``delay`` publica un mensaje en Redis y devuelve
    sin esperar a Groq. Otro proceso, el worker de Celery, consumira el mensaje.
    """

    task = analyze_cv_task.delay(
        analysis_id=payload.analysis_id,
        candidate_id=payload.candidate_id,
        cv_text=payload.cv_text,
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
