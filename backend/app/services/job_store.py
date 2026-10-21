"""Estado propio de extracción, independiente de AsyncResult de Celery."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import redis

from app.core.config import settings


_RENEW_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LOCK_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_UPDATE_OWNED_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[2], ARGV[4], 'EX', ARGV[2])
  redis.call('EXPIRE', KEYS[1], ARGV[3])
  return 1
end
return 0
"""


class LockOwnershipLost(RuntimeError):
    """El worker ya no posee el lease de extracción que inició."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExtractionJobStore:
    def __init__(self, client: redis.Redis | None = None) -> None:
        self.client = client or redis.from_url(settings.JOB_STORE_REDIS_URL, decode_responses=True)
        self._lock_tokens: dict[str, str] = {}

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

    def update_owned(self, job_id: str, **changes: Any) -> dict[str, Any]:
        """Atomically persist a job transition only while this worker owns its lease."""
        token = self._lock_tokens.get(job_id)
        if token is None:
            raise LockOwnershipLost("El worker ya no posee el lease de extracción.")
        job = self.get(job_id)
        if job is None:
            raise LockOwnershipLost("El trabajo expiró antes de guardar el progreso.")
        job.update(changes)
        job["updated_at"] = _now()
        updated = self.client.eval(
            _UPDATE_OWNED_LUA,
            2,
            f"ien-extraction:lock:{job_id}",
            self._job_key(job_id),
            token,
            settings.JOB_TTL_SECONDS,
            600,
            json.dumps(job),
        )
        if not updated:
            raise LockOwnershipLost("Se perdió el lease antes de guardar el trabajo.")
        return job

    def acquire_lock(self, job_id: str) -> bool:
        token = uuid4().hex
        acquired = bool(self.client.set(f"ien-extraction:lock:{job_id}", token, nx=True, ex=600))
        if acquired:
            self._lock_tokens[job_id] = token
        return acquired

    def renew_lock(self, job_id: str) -> bool:
        token = self._lock_tokens.get(job_id)
        if token is None:
            return False
        return bool(self.client.eval(_RENEW_LOCK_LUA, 1, f"ien-extraction:lock:{job_id}", token, 600))

    def release_lock(self, job_id: str) -> None:
        token = self._lock_tokens.pop(job_id, None)
        if token is not None:
            self.client.eval(_RELEASE_LOCK_LUA, 1, f"ien-extraction:lock:{job_id}", token)
