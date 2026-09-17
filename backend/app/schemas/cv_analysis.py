from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

class CVAnalysisStatus(str, Enum):
    """Estados internos de una tarea, independientes de los nombres de Celery."""

    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"

class CVAnalysisRequest(BaseModel):
    """Cuerpo JSON que la aplicacion origen envia a ``POST /cv-analyses``."""

    analysis_id: int = Field(gt=0)
    candidate_id: int = Field(gt=0)
    cv_text: str = Field(min_length=1)


class CVAnalysisAccepted(BaseModel):
    """Respuesta inmediata: el analisis todavia no termino."""

    # Celery crea ids de tarea como texto UUID. No lo convertimos a UUID para
    # aceptar tambien ids validos que una implementacion distinta de Celery use.
    job_id: str
    status: CVAnalysisStatus


class CVAnalysisDetail(CVAnalysisAccepted):
    """Estado consultable de una tarea encolada."""

    candidate_id: int
    result: dict[str, Any] | None = None
    error: str | None = None


class CVAnalysisTaskStatus(BaseModel):
    """Respuesta de ``GET /cv-analyses/{job_id}``.

    Mientras Celery esta procesando, todavia no hay resultado ni candidate_id
    disponible en el backend de resultados; por eso este modelo los deja fuera.
    El callback final siempre incluye ambos datos.
    """

    job_id: str
    status: CVAnalysisStatus
    result: dict[str, Any] | None = None
    error: str | None = None
