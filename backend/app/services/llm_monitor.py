"""Read scheduling budgets without reserving tokens or exposing credentials."""
import math
import time
from app.core.config import settings
from app.services import usage_metrics
from app.services.llm_router import routes, _limiter
from app.services.gemini_client import gemini_rate_limiter
from app.services.gemini_budget import GeminiBudgetLimiter


def snapshot():
    result = usage_metrics.snapshot()
    connection = usage_metrics.client()
    now = int(time.time() * 1000)
    candidates = {}
    for purpose in ('cv', 'match'):
        try:
            configured = routes('auto', purpose)
        except ValueError:
            configured = []
        for route in configured:
            candidates.setdefault(route.scope, route)
    result['routes'] = []
    for position, route in enumerate(list(candidates.values())[:50], 1):
        limiter = gemini_rate_limiter(route.model, route.key) if route.provider == 'gemini' else _limiter(route)
        credential = _limiter(route, credential=True)
        cooldown = max(float(connection.get(limiter.cooldown_key) or 0),
                       float(connection.get(credential.cooldown_key) or 0))
        row = {'provider': route.provider, 'model': route.model, 'slot': position,
               'cooldown_seconds': max(0, math.ceil((cooldown-now)/1000))}
        if route.provider == 'gemini':
            budget = GeminiBudgetLimiter(connection, limiter.cooldown_key.removeprefix('ien-extraction:cooldown:'),
                                         settings.GEMINI_REQUESTS_PER_MINUTE, settings.GEMINI_INPUT_TOKENS_PER_MINUTE,
                                         settings.GEMINI_REQUESTS_PER_DAY, settings.GEMINI_MAX_INPUT_TOKENS_PER_REQUEST)
            members = connection.zrangebyscore(budget.minute_key, now-60000, now)
            reserved_tokens = connection.hmget(budget.tokens_key, members) if members else []
            row.update(requests_minute=len(members), requests_minute_limit=budget.rpm,
                       input_tokens_minute=sum(int(value or 0) for value in reserved_tokens),
                       input_tokens_minute_limit=budget.tpm,
                       requests_day=connection.zcount(budget.daily_key, now-86400000, now),
                       requests_day_limit=budget.rpd)
        else:
            quota = connection.hgetall(limiter.quota_key)
            reset = float(quota.get('reset_at', 0))
            row.update(requests_minute=connection.zcount(limiter.rate_key, now-60000, now),
                       requests_minute_limit=limiter.requests_per_minute,
                       reported_remaining_tokens=int(quota['remaining_tokens']) if reset>now and 'remaining_tokens' in quota else None,
                       reported_reset_seconds=max(0, math.ceil((reset-now)/1000)) if reset>now else None)
        result['routes'].append(row)
    result['vercel_enabled'] = settings.AI_GATEWAY_ENABLED
    return result
