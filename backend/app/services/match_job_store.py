"""Registro recuperable e idempotente de trabajos de compatibilidad."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import redis

from ien_contracts.utils import content_hash

from app.core.config import settings
from app.services.job_store import LockOwnershipLost


_UPDATE_OWNED_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[2], ARGV[4], 'EX', ARGV[2])
  redis.call('EXPIRE', KEYS[1], ARGV[3])
  return 1
end
return 0
"""

_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MatchJobStore:
    def __init__(self, client: redis.Redis | None = None) -> None:
        self.client = client or redis.from_url(settings.MATCH_REDIS_URL, decode_responses=True)
        self._lock_tokens: dict[str, str] = {}

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
        inserted = self.client.set(self._job_key(request_id), json.dumps(job), ex=settings.JOB_TTL_SECONDS, nx=True)
        if not inserted:
            # The job and its mapping are separate keys. A concurrent request
            # may have persisted its job just before publishing the mapping.
            job = self.get(request_id)
            if job is None:
                raise RuntimeError("The match job expired before its mapping was saved.")
            if job["request_hash"] != request_hash:
                raise ValueError("The request_id was already used with another payload.")
        if not self.client.set(mapping_key, request_id, ex=settings.JOB_TTL_SECONDS, nx=True):
            return self.create_or_get(request)
        return job, bool(inserted)

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
        token = uuid4().hex
        acquired = bool(self.client.set(f"jev-match:lock:{request_id}", token, nx=True, ex=600))
        if acquired:
            self._lock_tokens[request_id] = token
        return acquired

    def update_owned(self, request_id: str, **changes: Any) -> dict[str, Any]:
        token = self._lock_tokens.get(request_id)
        if token is None:
            raise LockOwnershipLost("El worker ya no posee el lease de compatibilidad.")
        job = self.get(request_id)
        if job is None:
            raise LockOwnershipLost("La solicitud expiró antes de guardar el resultado.")
        job.update(changes)
        job["updated_at"] = utc_now()
        updated = self.client.eval(
            _UPDATE_OWNED_LUA, 2, f"jev-match:lock:{request_id}", self._job_key(request_id),
            token, settings.JOB_TTL_SECONDS, 600, json.dumps(job),
        )
        if not updated:
            raise LockOwnershipLost("Se perdió el lease antes de guardar la compatibilidad.")
        return job

    def release_lock(self, request_id: str) -> None:
        token = self._lock_tokens.pop(request_id, None)
        if token is not None:
            self.client.eval(_RELEASE_LOCK_LUA, 1, f"jev-match:lock:{request_id}", token)
