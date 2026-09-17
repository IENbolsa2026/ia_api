"""Trabajo asincrono de analisis y entrega de CVs.

FastAPI importa esta tarea para encolarla. El worker de Celery importa el mismo
modulo para ejecutarla fuera del proceso web.
"""

import asyncio
import logging
from typing import Any

from app.core.celery_app import celery_app
from app.services.cv_pipeline import process_cv_text
from app.services.result_notifier import notify_cv_result


logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, name="app.tasks.analyze_cv")
def analyze_cv_task(
    self: Any,
    analysis_id: int,
    candidate_id: int,
    cv_text: str,
) -> dict[str, Any]:
    """Analiza un CV, avisa el resultado y devuelve el payload a Celery.

    ``bind=True`` entrega ``self`` con metadatos de la tarea, incluidos los
    reintentos. La funcion es normal (no ``async def``) porque Celery ejecuta
    tareas sincronicas; ``asyncio.run`` abre el loop necesario para reutilizar
    el pipeline asincronico ya existente.
    """

    try:
        analysis = asyncio.run(process_cv_text(cv_text))
        payload: dict[str, Any] = {
            "analysis_id": analysis_id,
            "job_id": self.request.id,
            "candidate_id": candidate_id,
            "status": "completed",
            "result": analysis,
            "error": None,
        }
        notify_cv_result(payload)
        return payload
    except Exception as exc:
        logger.exception("CV analysis failed for task %s", self.request.id)

        # Reintentar es util ante caidas transitorias de Groq, Redis o el
        # callback. El tiempo crece 1, 2 y 4 segundos para no golpear un
        # servicio que probablemente sigue momentaneamente caido.
        if self.request.retries < self.max_retries:
            countdown = 2**self.request.retries
            raise self.retry(exc=exc, countdown=countdown) from exc

        failure_payload: dict[str, Any] = {
            "analysis_id": analysis_id,
            "job_id": self.request.id,
            "candidate_id": candidate_id,
            "status": "failed",
            "result": None,
            "error": "No se pudo procesar el CV. Revisar los logs del worker.",
        }
        try:
            # Incluso tras un fallo final avisamos al sistema origen para que no
            # quede esperando una respuesta que nunca llegara.
            notify_cv_result(failure_payload)
        except Exception:
            logger.exception("Could not deliver failure callback for task %s", self.request.id)

        # Celery registra la tarea como FAILURE; el GET devolvera ese estado.
        raise
