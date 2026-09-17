"""Cliente HTTP saliente para entregar el resultado a la aplicacion origen."""

from typing import Any

import httpx

from app.core.config import settings


def notify_cv_result(payload: dict[str, Any]) -> None:
    """Hace POST del resultado final al callback configurado.

    Esta funcion es sincronica porque se llama desde una tarea sincronica de
    Celery. ``httpx`` lanza una excepcion para respuestas 4xx/5xx, permitiendo
    que la tarea aplique su politica de reintentos.
    """

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if settings.CV_RESULT_CALLBACK_SECRET:
        headers["X-Callback-Secret"] = settings.CV_RESULT_CALLBACK_SECRET

    # Siempre usar timeout: sin este limite, una aplicacion destino caida puede
    # ocupar workers indefinidamente y dejar de procesar nuevos CVs.
    with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
        response = client.post(
            str(settings.CV_RESULT_CALLBACK_URL),
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
