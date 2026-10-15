"""Registro recuperable e idempotente de trabajos de compatibilidad."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import redis

from ien_contracts.utils import content_hash

from app.core.config import settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MatchJobStore:
    def __init__(self, client: redis.Redis | None = None) -> None:
        self.client = client or redis.from_url(settings.MATCH_REDIS_URL, decode_responses=True)

    def _job_key(self, request_id: str) -> str:
        return f"jev-match:job:{request_id}"

    def _idempotency_key(self, request_id: str) -> str:
        return f"jev-match:idempotency:{request_id}"

    def create_or_get(self, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        request_id = str(request["request_id"])
        request_hash = content_hash(request)
        mapping_key = self._idempotency_key(request_id)
        current = self.client.get(mapping_key)
        if current:
            job = self.get(current)
            if job is None:
                raise RuntimeError("The match job mapping points to a missing job.")
            if job["request_hash"] != request_hash:
                raise ValueError("The request_id was already used with another payload.")
            return job, False

        job = {
            "request_id": request_id,
            "request_hash": request_hash,
            "status": "accepted",
            "request": request,
            "result": None,
            "error": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        self.client.set(self._job_key(request_id), json.dumps(job), ex=settings.JOB_TTL_SECONDS, nx=True)
        if not self.client.set(mapping_key, request_id, ex=settings.JOB_TTL_SECONDS, nx=True):
            return self.create_or_get(request)
        return job, True

    def get(self, request_id: str) -> dict[str, Any] | None:
        value = self.client.get(self._job_key(request_id))
        return json.loads(value) if value else None

    def update(self, request_id: str, **changes: Any) -> dict[str, Any]:
        job = self.get(request_id)
        if job is None:
            raise KeyError(request_id)
        job.update(changes)
        job["updated_at"] = utc_now()
        self.client.set(self._job_key(request_id), json.dumps(job), ex=settings.JOB_TTL_SECONDS)
        return job

    def acquire_lock(self, request_id: str) -> bool:
        return bool(self.client.set(f"jev-match:lock:{request_id}", "1", nx=True, ex=600))

    def release_lock(self, request_id: str) -> None:
        self.client.delete(f"jev-match:lock:{request_id}")
