"""Tareas separadas para inferencia y entrega de resultados."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from app.core.celery_app import celery_app
from app.services.cv_pipeline import normalize_candidate_profile, process_cv_text, process_offer_snapshot
from app.services.job_store import ExtractionJobStore
from app.services.pdf_text import extract_pdf_text
from app.services.rate_limiter import RateLimitExceeded
from app.services.result_notifier import notify_extraction_result


logger = logging.getLogger(__name__)


def _callback_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = job["payload"]
    return {
        "event_id": f"extraction-{job['job_id']}",
        "job_id": job["job_id"],
        "request_id": job["idempotency_key"],
        "analysis_id": payload.get("analysis_id"),
        "candidate_id": payload.get("candidate_id"),
        "offer_id": payload.get("offer_id"),
        "kind": payload["kind"],
        "source_hash": payload["source_hash"],
        "status": "completed" if job["status"] in {"completed", "delivering", "delivered"} else "failed",
        "result": job.get("result"),
        "error": job.get("error"),
    }


@celery_app.task(bind=True, max_retries=5, name="app.tasks.run_extraction")
def run_extraction_task(self: Any, job_id: str) -> dict[str, Any]:
    store = ExtractionJobStore()
    job = store.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "missing"}
    if job["status"] in {"completed", "delivering", "delivered"}:
        return job
    if not store.acquire_lock(job_id):
        return {"job_id": job_id, "status": "already_processing"}
    try:
        store.update(job_id, status="processing", error=None)
        payload = job["payload"]
        if payload["kind"] == "candidate_cv":
            text = extract_pdf_text(base64.b64decode(payload["cv_pdf_base64"], validate=True))
            result = asyncio.run(process_cv_text(text, int(payload["candidate_id"])))
        elif payload["kind"] == "job_offer":
            result = asyncio.run(process_offer_snapshot(payload["offer_snapshot"], int(payload["offer_id"])))
        elif payload["kind"] == "profile_normalization":
            result = asyncio.run(normalize_candidate_profile(payload["profile_snapshot"], int(payload["candidate_id"])))
        else:
            raise ValueError("Unsupported extraction kind.")
        store.update(job_id, status="completed", result=result, error=None)
        deliver_extraction_result_task.delay(job_id)
        return result
    except RateLimitExceeded as exc:
        store.update(job_id, status="rate_limited", error=None)
        raise self.retry(exc=exc, countdown=max(1, int(exc.retry_after_seconds) + 1)) from exc
    except Exception as exc:
        logger.exception("Extraction failed for %s", job_id)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=min(60, 2 ** self.request.retries)) from exc
        store.update(job_id, status="failed", error="The extraction service could not process this request.")
        deliver_extraction_result_task.delay(job_id)
        raise
    finally:
        store.release_lock(job_id)


@celery_app.task(bind=True, max_retries=8, name="app.tasks.deliver_extraction_result")
def deliver_extraction_result_task(self: Any, job_id: str) -> dict[str, Any]:
    store = ExtractionJobStore()
    job = store.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "missing"}
    if job["status"] == "delivered":
        return {"job_id": job_id, "status": "delivered"}
    try:
        store.update(job_id, status="delivering")
        notify_extraction_result(_callback_payload(job))
        store.update(job_id, status="delivered")
        return {"job_id": job_id, "status": "delivered"}
    except Exception as exc:
        logger.exception("Could not deliver extraction result for %s", job_id)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=min(300, 2 ** self.request.retries)) from exc
        store.update(job_id, status="delivery_failed", error="The result is ready but delivery to Bolsa failed.")
        raise
