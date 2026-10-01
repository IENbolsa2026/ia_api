"""Ventana móvil Redis para coordinar las llamadas a Groq entre workers."""

from __future__ import annotations

import time

import redis


LUA_RESERVE = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
if redis.call('ZCARD', key) >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  return {0, tonumber(oldest[2]) + window - now}
end
redis.call('ZADD', key, now, ARGV[4])
redis.call('PEXPIRE', key, window)
return {1, 0}
"""


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Provider rate limit reservation was not available.")


class RedisRateLimiter:
    def __init__(self, client: redis.Redis, requests_per_minute: int) -> None:
        self.client = client
        self.requests_per_minute = max(1, requests_per_minute)

    def reserve(self, member: str) -> None:
        now = int(time.time() * 1000)
        granted, wait_ms = self.client.eval(
            LUA_RESERVE, 1, "ien-extraction:rate:groq", now, 60_000, self.requests_per_minute, member
        )
        if not granted:
            raise RateLimitExceeded(max(1, int(wait_ms)) / 1000)
