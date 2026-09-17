import logging
from uuid import UUID

from app.services.cv_pipeline import process_cv_text
from app.services.job_store import (
    mark_job_completed,
    mark_job_failed,
    mark_job_processing,
)

logger = logging.getLogger(__name__)

async def run_cv_analysis(job_id: UUID, cv_text: str) -> None:
    mark_job_processing(job_id)

    try:
        result = await process_cv_text(cv_text)
    except Exception:
        logger.exception("CV analysis failed for job %s", job_id)
        mark_job_failed(
            job_id,
            "No se pudo procesar el CV. Revisá los logs del servidor.",
        )
        return

    mark_job_completed(job_id, result)