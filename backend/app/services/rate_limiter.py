"""Ventana móvil Redis para coordinar las llamadas a Groq entre workers."""

from __future__ import annotations

import time
import hashlib
import logging
import math
import re
from uuid import uuid4
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import redis

logger = logging.getLogger(__name__)


LUA_RESERVE = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local spacing = tonumber(ARGV[4])
local estimated_tokens = tonumber(ARGV[5])
local next_allowed = tonumber(redis.call('GET', KEYS[2])) or 0
if next_allowed > now then
  return {0, next_allowed - now, 1}
end
local remaining = tonumber(redis.call('HGET', KEYS[3], 'remaining_tokens'))
local reset_at = tonumber(redis.call('HGET', KEYS[3], 'reset_at')) or 0
if remaining and estimated_tokens > remaining and reset_at > now then
  return {0, reset_at - now, 2}
end
if limit > 0 then
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
if redis.call('ZCARD', key) >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  return {0, tonumber(oldest[2]) + window - now, 3}
end
redis.call('ZADD', key, now, ARGV[6])
redis.call('PEXPIRE', key, window)
end
if spacing > 0 then
  redis.call('SET', KEYS[2], now + spacing, 'PX', spacing)
end
return {1, 0, 0}
"""

LUA_RECORD_RESPONSE = """
local now = tonumber(ARGV[1])
local delay = tonumber(ARGV[2])
local current = tonumber(redis.call('GET', KEYS[1])) or 0
local target = math.max(current, now + delay)
if target > now then
  redis.call('SET', KEYS[1], target, 'PX', math.ceil(target - now))
end
if ARGV[3] ~= '' and ARGV[4] ~= '' then
  redis.call('HSET', KEYS[2], 'remaining_tokens', ARGV[3], 'reset_at', ARGV[4])
  redis.call('PEXPIRE', KEYS[2], math.max(60000, tonumber(ARGV[4]) - now))
end
return math.max(0, target - now)
"""


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: float, reason: str = "provider") -> None:
        self.retry_after_seconds = retry_after_seconds
        self.reason = reason
        self.is_local_wait = reason in {"cooldown", "token_headroom", "rpm", "daily_budget"}
        super().__init__("Provider rate limit reservation was not available.")


class ProviderOutputLimitExceeded(ValueError):
    """El proveedor rechazó permanentemente la salida por su cuota de tokens."""

    def __init__(self, limit_num: int | None = None) -> None:
        self.limit_num = limit_num
        super().__init__(
            "La capacidad actual del proveedor no permite completar el análisis. "
            "Ajustá la capacidad disponible o dividí el análisis en partes."
        )


class ProviderResponseTruncated(ValueError):
    """El proveedor terminó una respuesta JSON antes de completarla."""


def retry_after_seconds(value: str | None, default: int = 60) -> float:
    """Interpreta Retry-After (segundos o fecha HTTP) y limita valores extremos."""
    if value:
        try:
            return min(3600.0, max(1.0, float(value)))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                return min(3600.0, max(1.0, (target - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return float(default)


def reset_duration_seconds(value: str | None) -> float | None:
    """Parse Groq durations such as 7.66s or 2m59.56s, never timestamps."""
    if not value or len(value) > 80:
        return None
    normalized = value.strip().lower()
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", normalized)
    if not parts or "".join(number + unit for number, unit in parts) != normalized:
        return None
    factors = {"ms": .001, "s": 1, "m": 60, "h": 3600, "d": 86400}
    duration = sum(float(number) * factors[unit] for number, unit in parts)
    return min(86400.0, duration) if math.isfinite(duration) else None


def estimate_request_tokens(system: str, payload: str, output_tokens: int) -> int:
    # A conservative scheduling estimate, not a tokenizer or billing count.
    return math.ceil((len(system) + len(payload)) / 3) + max(0, output_tokens)


def _numeric_header(headers: dict[str, str], name: str) -> int | None:
    value = headers.get(name)
    if value is None or not re.fullmatch(r"\d{1,12}", value):
        return None
    return int(value)


def groq_rate_limiter(model: str) -> "RedisRateLimiter":
    from app.core.config import settings
    # Fingerprints distinguish credentials/models without storing either key.
    scope = hashlib.sha256(f"{settings.GROQ_API_KEY}\0{model}".encode()).hexdigest()[:32]
    return RedisRateLimiter(
        redis.from_url(settings.JOB_STORE_REDIS_URL, decode_responses=True),
        settings.GROQ_REQUESTS_PER_MINUTE,
        cooldown_scope=f"groq:{scope}",
        minimum_interval_seconds=settings.GROQ_PAUSA_SEGUNDOS,
    )


class RedisRateLimiter:
    def __init__(self, client: redis.Redis, requests_per_minute: int, *,
                 cooldown_scope: str = "groq", minimum_interval_seconds: float = 0,
                 provider: str = "groq") -> None:
        self.client = client
        self.requests_per_minute = max(1, requests_per_minute)
        self.minimum_interval_seconds = max(0, minimum_interval_seconds)
        self.rate_key = f"ien-extraction:rate:{provider}"
        self.cooldown_key = f"ien-extraction:cooldown:{cooldown_scope}"
        self.quota_key = f"ien-extraction:quota:{cooldown_scope}"

    def reserve(self, member: str, estimated_tokens: int = 0, *, count_request: bool = True) -> None:
        now = int(time.time() * 1000)
        result = self.client.eval(
            LUA_RESERVE, 3, self.rate_key, self.cooldown_key, self.quota_key,
            now, 60_000, self.requests_per_minute if count_request else 0,
            math.ceil(self.minimum_interval_seconds * 1000), max(0, estimated_tokens),
            f"{member}:{uuid4().hex}",
        )
        granted, wait_ms = result[:2]
        if not granted:
            reason = {1: "cooldown", 2: "token_headroom", 3: "rpm"}.get(result[2] if len(result) > 2 else 3, "rpm")
            wait = max(1, int(wait_ms)) / 1000
            logger.info("Provider scheduling wait reason=%s seconds=%.3f", reason, wait)
            raise RateLimitExceeded(wait, reason=reason)

    def defer(self, seconds: float) -> float:
        """Share a temporary provider outage cooldown without changing quota balances."""
        now = int(time.time() * 1000)
        wait_ms = self.client.eval(
            LUA_RECORD_RESPONSE, 2, self.cooldown_key, self.quota_key,
            now, math.ceil(seconds * 1000), '', '',
        )
        return max(seconds, float(wait_ms) / 1000)

    def record_response(self, headers, *, status_code: int, estimated_tokens: int = 0) -> float:
        # Only parse documented numeric/duration fields; never log header maps,
        # provider response bodies, request text or credentials.
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        remaining = _numeric_header(normalized, "x-ratelimit-remaining-tokens")
        remaining_requests = _numeric_header(normalized, "x-ratelimit-remaining-requests")
        reset_tokens = reset_duration_seconds(normalized.get("x-ratelimit-reset-tokens"))
        reset_requests = reset_duration_seconds(normalized.get("x-ratelimit-reset-requests"))
        wait = self.minimum_interval_seconds
        reason = "post_success_pause"
        if status_code == 429:
            wait = max(wait, retry_after_seconds(normalized.get("retry-after")))
            reason = "provider_429"
            if remaining is not None and remaining < max(1, estimated_tokens) and reset_tokens is not None:
                wait = max(wait, reset_tokens)
            # Groq's requests headers refer to RPD, not RPM. A positive
            # daily balance must never impose its reset on a normal 429.
        if remaining_requests == 0 and reset_requests is not None:
            wait = max(wait, reset_requests)
        now = int(time.time() * 1000)
        actual_wait_ms = self.client.eval(
            LUA_RECORD_RESPONSE, 2, self.cooldown_key, self.quota_key,
            now, math.ceil(wait * 1000),
            remaining if remaining is not None else "",
            now + math.ceil(reset_tokens * 1000) if reset_tokens is not None else "",
        )
        logger.info(
            "Provider quota status=%d reason=%s seconds=%.3f remaining_tokens=%s reset_tokens_seconds=%s",
            status_code, reason, wait, remaining, reset_tokens,
        )
        return max(wait, float(actual_wait_ms) / 1000)
