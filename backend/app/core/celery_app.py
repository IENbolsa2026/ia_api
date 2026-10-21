"""Instancia compartida de Celery.

Este modulo no ejecuta tareas por si mismo. Define como todos los procesos se
conectan a Redis: FastAPI publica tareas y el comando ``celery worker`` las
consume usando exactamente esta misma configuracion.
"""

from celery import Celery

from app.core.config import settings


celery_app = Celery(
    "cv_analyzer",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    # Registra las tareas de forma explicita. Asi Celery sabe que modulos debe
    # importar cuando el worker inicia.
    include=["app.tasks.cv_analysis_tasks", "app.tasks.match_tasks"],
    # Permite mostrar STARTED en el endpoint GET, no solo PENDING y SUCCESS.
    task_track_started=True,
    # Los jobs se guardan antes de encolarse y sus workers son idempotentes.
    # Con ACK temprano, una caída del proceso perdería una extracción en curso.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # JSON es portable entre Redis y evita serializar objetos Python arbitrarios.
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # El resultado queda disponible una semana para consultar el GET. Ajustalo
    # segun la politica de retencion que necesite tu sistema.
    result_expires=60 * 60 * 24 * 7,
)
