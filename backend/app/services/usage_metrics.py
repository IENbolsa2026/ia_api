"""Bounded hourly usage counters. Store numbers and model names, never CVs or keys."""
from datetime import datetime, timezone
import logging
import re
import time

import redis
from app.core.config import settings

logger = logging.getLogger(__name__)
PREFIX = 'ien:llm-usage:v1'
FIELDS = ('requests', 'errors', 'rate_limited', 'usage_responses', 'prompt_tokens',
          'completion_tokens', 'total_tokens', 'thought_tokens', 'cached_tokens')
RETENTION = 8 * 86400


def client():
    return redis.from_url(settings.JOB_STORE_REDIS_URL, decode_responses=True,
                          socket_connect_timeout=1, socket_timeout=1)


def record_response(provider, model, status, usage=None):
    if provider not in {'gemini', 'groq', 'vercel'} or not re.fullmatch(r'[A-Za-z0-9._/-]{1,120}', model):
        return
    values = {key: value for key, value in (usage or {}).items()
              if key in FIELDS[4:] and type(value) is int and 0 <= value <= 10**12}
    now = int(time.time())
    hour = now // 3600 * 3600
    key = f'{PREFIX}:{hour}:{provider}:{model}'
    increments = {'requests': 1, 'errors': int(status == 0 or status >= 400),
                  'rate_limited': int(status == 429), 'usage_responses': int(bool(values)), **values}
    try:
        pipe = client().pipeline()
        pipe.hset(key, mapping={'provider': provider, 'model': model, 'hour': hour})
        for field, value in increments.items():
            pipe.hincrby(key, field, value)
        pipe.expire(key, RETENTION)
        pipe.zadd(f'{PREFIX}:index', {key: hour})
        pipe.zremrangebyscore(f'{PREFIX}:index', '-inf', now - RETENTION)
        pipe.expire(f'{PREFIX}:index', RETENTION)
        pipe.set(f'{PREFIX}:started', now, nx=True)
        pipe.execute()
    except redis.RedisError:
        # A monitoring failure must never repeat or fail a paid model request.
        logger.warning('LLM usage counters unavailable')


def snapshot():
    now = int(time.time())
    first_hour = now // 3600 * 3600 - 23 * 3600
    connection = client()
    keys = connection.zrangebyscore(f'{PREFIX}:index', first_hour, now, start=0, num=2500)
    pipe = connection.pipeline(transaction=False)
    for key in keys:
        pipe.hgetall(key)
    rows = pipe.execute() if keys else []
    totals = dict.fromkeys(FIELDS, 0)
    models = {}
    hours = {hour: dict.fromkeys(FIELDS, 0) for hour in range(first_hour, first_hour + 24 * 3600, 3600)}
    for row in rows:
        if not row or row.get('provider') not in {'gemini', 'groq', 'vercel'}:
            continue
        model = row.get('model', '')
        if not re.fullmatch(r'[A-Za-z0-9._/-]{1,120}', model):
            continue
        hour = int(row['hour'])
        if hour not in hours:
            continue
        identity = (row['provider'], model)
        group = models.setdefault(identity, {'provider': identity[0], 'model': model, **dict.fromkeys(FIELDS, 0)})
        for field in FIELDS:
            value = max(0, int(row.get(field, 0)))
            totals[field] += value
            group[field] += value
            hours[hour][field] += value
    return {
        'generated_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
        'started_at': connection.get(f'{PREFIX}:started'),
        'totals': totals, 'models': sorted(models.values(), key=lambda row: (row['provider'], row['model'])),
        'hours': [{'at': datetime.fromtimestamp(hour, timezone.utc).isoformat(), **values}
                  for hour, values in hours.items()],
    }
