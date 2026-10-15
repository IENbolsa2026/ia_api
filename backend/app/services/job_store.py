"""Estado propio de extracción, independiente de AsyncResult de Celery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import redis

from app.core.config import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExtractionJobStore:
    def __init__(self, client: redis.Redis | None = None) -> None:
        self.client = client or redis.from_url(settings.JOB_STORE_REDIS_URL, decode_responses=True)

    @staticmethod
    def _job_key(job_id: str) -> str:
        return f"ien-extraction:job:{job_id}"

    @staticmethod
    def _idempotency_key(key: str) -> str:
        return f"ien-extraction:idempotency:{key}"

    def create_or_get(self, key: str, payload_hash: str, payload: dict[str, Any], job_id: str) -> tuple[dict[str, Any], bool]:
        existing_id = self.client.get(self._idempotency_key(key))
        if existing_id:
            job = self.get(existing_id)
            if job is None:
                raise RuntimeError("The extraction idempotency mapping is inconsistent.")
            if job["payload_hash"] != payload_hash:
                raise ValueError("Idempotency-Key was already used with another payload.")
            return job, False
        job = {
            "job_id": job_id,
            "idempotency_key": key,
            "payload_hash": payload_hash,
            "payload": payload,
            "status": "accepted",
            "result": None,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.client.set(self._job_key(job_id), json.dumps(job), ex=settings.JOB_TTL_SECONDS, nx=True)
        if not self.client.set(self._idempotency_key(key), job_id, ex=settings.JOB_TTL_SECONDS, nx=True):
            return self.create_or_get(key, payload_hash, payload, job_id)
        return job, True

    def get(self, job_id: str) -> dict[str, Any] | None:
        value = self.client.get(self._job_key(job_id))
        return json.loads(value) if value else None

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        job.update(changes)
        job["updated_at"] = _now()
        self.client.set(self._job_key(job_id), json.dumps(job), ex=settings.JOB_TTL_SECONDS)
        return job

    def acquire_lock(self, job_id: str) -> bool:
        return bool(self.client.set(f"ien-extraction:lock:{job_id}", "1", nx=True, ex=600))

    def release_lock(self, job_id: str) -> None:
        self.client.delete(f"ien-extraction:lock:{job_id}")
