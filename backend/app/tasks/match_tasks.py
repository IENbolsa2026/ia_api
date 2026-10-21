from __future__ import annotations

import logging
from typing import Any

from celery.exceptions import Retry

from app.core.celery_app import celery_app
from app.core.config import settings
from app.services.match_job_store import MatchJobStore
from app.services.job_store import LockOwnershipLost
from app.services.match_groq import GroqClient
from app.services.rate_limiter import RateLimitExceeded
from app.services.match_scoring import build_result, candidate_evidence_index, questions


logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=5, name="app.tasks.evaluate_match")
def evaluate_match_task(self: Any, request_id: str) -> dict[str, Any]:
    store = MatchJobStore()
    job = store.get(request_id)
    if not job:
        return {"request_id": request_id, "status": "missing"}
    if job["status"] == "completed":
        return job["result"]
    if job["status"] == "failed":
        return {"request_id": request_id, "status": "failed"}
    if not store.acquire_lock(request_id):
        # No se confirma una redelivery mientras otro worker conserva el
        # lease: si ese worker cayó, este reintento retomará el trabajo al
        # expirar el lock sin reservar cuota ni volver a llamar al proveedor.
        countdown = min(600, 30 * (2 ** max(0, int(self.request.retries))))
        raise self.retry(
            exc=RuntimeError("La evaluación sigue bloqueada por otra ejecución."),
            countdown=countdown,
        )
    def update_owned(**changes: Any) -> None:
        try:
            store.update_owned(request_id, **changes)
        except LockOwnershipLost as exc:
            raise self.retry(exc=exc, countdown=30, max_retries=max(10, self.request.retries + 1)) from exc

    try:
        # A redelivery may have read the job just before its owner finished.
        job = store.get(request_id)
        if not job:
            return {"request_id": request_id, "status": "missing"}
        if job["status"] == "completed":
            return job["result"]
        if job["status"] == "failed":
            return {"request_id": request_id, "status": "failed"}
        update_owned(status="processing", error=None)
        request = job["request"]
        if request.get("protocol_version") == "match.v2":
            response = GroqClient().evaluate_v2({
                "candidate_profile": request["candidate_profile"],
                "job_offer": request["job_offer"],
                "evaluation_date": request["metadata"]["evaluation_date"],
                "candidate_evidence_index": candidate_evidence_index(request["candidate_profile"]),
            })
        else:
            response = GroqClient().evaluate(
                {"candidate_profile": request["candidate_profile"], "job_offer": request["job_offer"]},
                questions(),
            )
        result = build_result(request, response).model_dump(mode="json")
        update_owned(status="completed", result=result, provider_model=response.get("model", (settings.MATCH_GROQ_MODEL or settings.CV_MODEL)), usage=response.get("usage", {}))
        return result
    except Retry:
        raise
    except RateLimitExceeded as exc:
        if exc.is_local_wait:
            update_owned(status='rate_limited')
            raise self.retry(
                exc=exc, countdown=max(1, int(exc.retry_after_seconds) + 1),
                max_retries=max(1000, self.request.retries + 1),
            ) from exc
        failures = int(job.get('provider_failures', 0)) + 1
        if failures >= self.max_retries:
            update_owned(status='failed', provider_failures=failures, error='No se pudo obtener cuota para completar la evaluación.')
            raise
        update_owned(status='rate_limited', provider_failures=failures)
        raise self.retry(exc=exc, countdown=max(1, int(exc.retry_after_seconds) + 1), max_retries=max(1000, self.request.retries + 1)) from exc
    except self.MaxRetriesExceededError:
        update_owned(status="failed", error="No se pudo completar la evaluación de compatibilidad.")
        raise
    except Exception as exc:
        logger.error("Match evaluation failed for request %s (%s)", request_id, type(exc).__name__)
        failures = int(job.get('provider_failures', 0)) + 1
        if failures < self.max_retries:
            update_owned(provider_failures=failures)
            raise self.retry(exc=exc, countdown=min(60, 2 ** failures), max_retries=max(1000, self.request.retries + 1)) from exc
        update_owned(status="failed", provider_failures=failures, error="No se pudo completar la evaluación de compatibilidad.")
        raise
    finally:
        store.release_lock(request_id)
