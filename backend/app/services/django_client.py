import logging

import httpx

from app.config import settings
from app.schemas.analisis import DjangoWebhookPayload, EstadoTarea

logger = logging.getLogger(__name__)


class DjangoClientError(Exception):
    """Error al avisar a Django. No conoce nada de FastAPI."""


def _enviar(payload: DjangoWebhookPayload) -> bool:
    url = settings.DJANGO_WEBHOOK_URL
    if not url:
        print(f"[FLUJO] django_client: DJANGO_WEBHOOK_URL vacía → se omite el aviso (job {payload.job_id})")
        logger.warning(
            "DJANGO_WEBHOOK_URL no está configurada; se omite el aviso para job %s.",
            payload.job_id,
        )
        return False

    datos = payload.model_dump(mode="json")
    try:
        with httpx.Client(timeout=15.0) as client:
            respuesta = client.post(url, json=datos)
        respuesta.raise_for_status()
        print(f"[FLUJO] django_client: POST {url} → HTTP {respuesta.status_code}")
        logger.info("Django notificado para job %s (HTTP %s).", payload.job_id, respuesta.status_code)
        return True
    except httpx.HTTPError as exc:
        logger.error("Fallo el webhook a Django para job %s: %s", payload.job_id, exc)
        raise DjangoClientError(f"No se pudo notificar a Django: {exc}") from exc


def notificar_resultado(
    *,
    job_id: str,
    filename: str,
    resultado: dict,
) -> bool:
    return _enviar(
        DjangoWebhookPayload(
            job_id=job_id,
            filename=filename,
            estado=EstadoTarea.completado.value,
            resultado=resultado,
        )
    )


def notificar_error(*, job_id: str, filename: str, error: str) -> bool:
    return _enviar(
        DjangoWebhookPayload(
            job_id=job_id,
            filename=filename,
            estado=EstadoTarea.error.value,
            error=error,
        )
    )