"""Atomic Redis admission budget for Gemini requests.

The limits are conservative local scheduling controls, not statements about a
provider account's published quota. Input usage is estimated from characters;
no provider token-count request is made before admission.
"""

from __future__ import annotations

import math
from uuid import uuid4

import redis

from app.services.rate_limiter import RateLimitExceeded


LUA_RESERVE_GEMINI = r"""
local minute_key = KEYS[1]
local daily_key = KEYS[2]
local tokens_key = KEYS[3]
local cooldown_key = KEYS[4]
local rpm = tonumber(ARGV[1])
local tpm = tonumber(ARGV[2])
local rpd = tonumber(ARGV[3])
local max_request_tokens = tonumber(ARGV[4])
local input_tokens = tonumber(ARGV[5])
local member = ARGV[6]

local server_time = redis.call('TIME')
local now = tonumber(server_time[1]) * 1000 + math.floor(tonumber(server_time[2]) / 1000)
local minute_window = 60000
local day_window = 86400000

if input_tokens > max_request_tokens then
  return {0, minute_window, 2}
end

local cooldown_until = tonumber(redis.call('GET', cooldown_key)) or 0
if cooldown_until > now then
  return {0, cooldown_until - now, 4}
end

local expired_daily = redis.call('ZRANGEBYSCORE', daily_key, '-inf', now - day_window)
for _, old_member in ipairs(expired_daily) do
  redis.call('HDEL', tokens_key, old_member)
  redis.call('ZREM', minute_key, old_member)
end
redis.call('ZREMRANGEBYSCORE', daily_key, '-inf', now - day_window)
redis.call('ZREMRANGEBYSCORE', minute_key, '-inf', now - minute_window)

if rpd > 0 and redis.call('ZCARD', daily_key) >= rpd then
  local oldest = redis.call('ZRANGE', daily_key, 0, 0, 'WITHSCORES')
  return {0, tonumber(oldest[2]) + day_window - now, 3}
end

local minute_members = redis.call('ZRANGE', minute_key, 0, -1, 'WITHSCORES')
if rpm > 0 and #minute_members / 2 >= rpm then
  return {0, tonumber(minute_members[2]) + minute_window - now, 1}
end

if tpm > 0 then
  local current_tokens = 0
  local entries = {}
  for i = 1, #minute_members, 2 do
    local token_count = tonumber(redis.call('HGET', tokens_key, minute_members[i])) or 0
    current_tokens = current_tokens + token_count
    table.insert(entries, {tonumber(minute_members[i + 1]), token_count})
  end
  if current_tokens + input_tokens > tpm then
    local remaining = current_tokens
    local wait_ms = minute_window
    for _, entry in ipairs(entries) do
      remaining = remaining - entry[2]
      if remaining + input_tokens <= tpm then
        wait_ms = entry[1] + minute_window - now
        break
      end
    end
    return {0, math.max(1, wait_ms), 2}
  end
end

redis.call('ZADD', minute_key, now, member)
redis.call('ZADD', daily_key, now, member)
redis.call('HSET', tokens_key, member, input_tokens)
redis.call('PEXPIRE', minute_key, minute_window + 1000)
redis.call('PEXPIRE', daily_key, day_window + 1000)
redis.call('PEXPIRE', tokens_key, day_window + 1000)
return {1, 0, 0}
"""


def estimate_input_tokens(text: str) -> int:
    """Estimate input tokens conservatively as ceil(characters / 2)."""
    return math.ceil(len(text) / 2)


class GeminiBudgetLimiter:
    """Reserve Gemini RPM, input TPM, and rolling daily requests atomically."""

    def __init__(
        self,
        client: redis.Redis,
        scope: str,
        rpm: int,
        tpm: int,
        rpd: int,
        max_input_tokens: int = 12000,
    ) -> None:
        # ``scope`` should match RedisRateLimiter's cooldown_scope, e.g. a
        # provider/key/model fingerprint, so provider 429 cooldowns are shared.
        prefix = f"ien-extraction:gemini-budget:{scope}"
        self.client = client
        self.minute_key = f"{prefix}:minute"
        self.daily_key = f"{prefix}:daily"
        self.tokens_key = f"{prefix}:tokens"
        self.cooldown_key = f"ien-extraction:cooldown:{scope}"
        self.rpm = max(1, int(rpm))
        self.tpm = max(1, int(tpm))
        self.rpd = max(1, int(rpd))
        self.max_input_tokens = max(1, int(max_input_tokens))

    def reserve(self, marker: str, input_tokens: int) -> None:
        """Atomically reserve one request and its estimated input tokens.

        ``marker`` is accepted for call-site compatibility and observability,
        but is deliberately not persisted because it may contain user data.
        """
        del marker
        estimate = max(0, int(input_tokens))
        if estimate > min(self.max_input_tokens, self.tpm):
            raise ValueError('Estimated input exceeds the local per-request token budget.')
        member = uuid4().hex
        result = self.client.eval(
            LUA_RESERVE_GEMINI,
            4,
            self.minute_key,
            self.daily_key,
            self.tokens_key,
            self.cooldown_key,
            self.rpm,
            self.tpm,
            self.rpd,
            self.max_input_tokens,
            estimate,
            member,
        )
        granted, wait_ms, reason_code = result
        if not granted:
            reason = {
                1: 'rpm',
                2: 'token_headroom',
                3: 'daily_budget',
                4: 'cooldown',
            }.get(int(reason_code), 'rpm')
            raise RateLimitExceeded(max(0.001, float(wait_ms) / 1000), reason=reason)
