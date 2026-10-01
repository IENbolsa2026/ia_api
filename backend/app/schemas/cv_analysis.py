from enum import Enum
from typing import Any

from pydantic import BaseModel

class CVAnalysisStatus(str, Enum):
    """Estados internos de una tarea, independientes de los nombres de Celery."""

    queued = "queued"
    accepted = "accepted"
    rate_limited = "rate_limited"
    processing = "processing"
    completed = "completed"
    failed = "failed"

class CVAnalysisAccepted(BaseModel):
    """Respuesta inmediata: el analisis todavia no termino."""

    # Celery crea ids de tarea como texto UUID. No lo convertimos a UUID para
    # aceptar tambien ids validos que una implementacion distinta de Celery use.
    job_id: str
    status: CVAnalysisStatus
    request_id: str | None = None


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
    delivered: bool = False
