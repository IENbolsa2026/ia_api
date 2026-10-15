from __future__ import annotations

import logging
from typing import Any

import redis

from app.core.celery_app import celery_app
from app.core.config import settings
from app.services.match_job_store import MatchJobStore
from app.services.match_groq import GroqClient
from app.services.rate_limiter import RedisRateLimiter, RateLimitExceeded
from app.services.match_scoring import build_result, questions


logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=5, name="app.tasks.evaluate_match")
def evaluate_match_task(self: Any, request_id: str) -> dict[str, Any]:
    store = MatchJobStore()
    job = store.get(request_id)
    if not job:
        return {"request_id": request_id, "status": "missing"}
    if job["status"] == "completed":
        return job["result"]
    if not store.acquire_lock(request_id):
        return {"request_id": request_id, "status": "already_processing"}
    try:
        store.update(request_id, status="processing", error=None)
        limiter = RedisRateLimiter(redis.from_url(settings.JOB_STORE_REDIS_URL, decode_responses=True), settings.GROQ_REQUESTS_PER_MINUTE)
        limiter.reserve(f'match-{request_id}-{self.request.retries}')
        request = job["request"]
        response = GroqClient().evaluate(
            {"candidate_profile": request["candidate_profile"], "job_offer": request["job_offer"]},
            questions(),
        )
        result = build_result(request, response).model_dump(mode="json")
        store.update(request_id, status="completed", result=result, provider_model=response.get("model", (settings.MATCH_GROQ_MODEL or settings.CV_MODEL)), usage=response.get("usage", {}))
        return result
    except RateLimitExceeded as exc:
        if self.request.retries >= self.max_retries:
            store.update(request_id, status='failed', error='No se pudo obtener cuota para completar la evaluación.')
            raise
        store.update(request_id, status='rate_limited')
        raise self.retry(exc=exc, countdown=max(1, int(exc.retry_after_seconds) + 1)) from exc
    except self.MaxRetriesExceededError:
        store.update(request_id, status="failed", error="The compatibility service could not complete the evaluation.")
        raise
    except Exception as exc:
        logger.exception("Match evaluation failed for %s", request_id)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=min(60, 2 ** self.request.retries)) from exc
        store.update(request_id, status="failed", error="The compatibility service could not complete the evaluation.")
        raise
    finally:
        store.release_lock(request_id)
