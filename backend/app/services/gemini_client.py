"""Gemini JSON generation with isolated scheduling and safe provider errors."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import random
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx
import redis

from app.core.config import settings
from app.services.gemini_cv_schema import cv_response_schema
from app.services.gemini_budget import GeminiBudgetLimiter, estimate_input_tokens
from app.services.rate_limiter import (
    LUA_RECORD_RESPONSE, ProviderResponseTruncated, RateLimitExceeded, RedisRateLimiter,
    reset_duration_seconds, retry_after_seconds,
)


logger = logging.getLogger(__name__)


class GeminiError(RuntimeError):
    pass


class GeminiUnavailable(GeminiError):
    """Temporary service/transport failure; one HTTP attempt per task admission."""

    def __init__(self, retry_after_seconds: float = 30) -> None:
        self.retry_after_seconds = retry_after_seconds
        # Keep constructor arguments serializable when Celery transports this error.
        super().__init__(retry_after_seconds)

    def __str__(self) -> str:
        return 'Gemini no está disponible temporalmente. El análisis se reintentará.'

    def retry_countdown(self, failures: int) -> int:
        backoff = min(300, 30 * 2 ** min(4, max(0, failures - 1)))
        return math.ceil(max(self.retry_after_seconds, backoff) + random.uniform(0, 5))


class GeminiRateLimiter(RedisRateLimiter):
    def reserve(self, member: str, estimated_tokens: int = 0, *, count_request: bool = True) -> None:
        budget = GeminiBudgetLimiter(
            self.client, self.cooldown_key.removeprefix('ien-extraction:cooldown:'),
            settings.GEMINI_REQUESTS_PER_MINUTE,
            settings.GEMINI_INPUT_TOKENS_PER_MINUTE,
            settings.GEMINI_REQUESTS_PER_DAY,
            settings.GEMINI_MAX_INPUT_TOKENS_PER_REQUEST,
        )
        budget.reserve(member, estimated_tokens)


def gemini_rate_limiter(model: str) -> RedisRateLimiter:
    scope = hashlib.sha256(f'{settings.GEMINI_API_KEY}\0{model}'.encode()).hexdigest()[:32]
    return GeminiRateLimiter(
        redis.from_url(settings.JOB_STORE_REDIS_URL, decode_responses=True),
        settings.GEMINI_REQUESTS_PER_MINUTE, provider='gemini',
        cooldown_scope=f'gemini:{scope}', minimum_interval_seconds=settings.GEMINI_PAUSA_SEGUNDOS,
    )


def _retry_delay(response: httpx.Response, default: float = 60.0) -> float:
    values = []
    header = response.headers.get('retry-after')
    if header:
        values.append(retry_after_seconds(header))
    try:
        body = response.json()
        error = body.get('error', {}) if isinstance(body, dict) else {}
        details = error.get('details', []) if isinstance(error, dict) else []
        for detail in details if isinstance(details, list) else []:
            if not isinstance(detail, dict) or detail.get('@type') != 'type.googleapis.com/google.rpc.RetryInfo':
                continue
            delay = detail.get('retryDelay')
            if isinstance(delay, str):
                seconds = reset_duration_seconds(delay)
            elif isinstance(delay, dict):
                try:
                    seconds = float(delay.get('seconds', 0)) + float(delay.get('nanos', 0)) / 1_000_000_000
                except (ValueError, TypeError, OverflowError):
                    seconds = None
            else:
                seconds = None
            if seconds is not None and math.isfinite(seconds) and seconds > 0:
                values.append(min(86400.0, seconds))
    except (ValueError, TypeError):
        pass
    return max(values) if values else default


def _daily_quota_exhausted(response: httpx.Response) -> bool:
    try:
        details = response.json().get('error', {}).get('details', [])
        for detail in details:
            if not isinstance(detail, dict) or detail.get('@type') != 'type.googleapis.com/google.rpc.QuotaFailure':
                continue
            for violation in detail.get('violations', []):
                if not isinstance(violation, dict):
                    continue
                quota = str(violation.get('quotaId', '')) + str(violation.get('quotaMetric', ''))
                if 'perday' in quota.casefold() or 'per_day' in quota.casefold():
                    return True
    except (ValueError, TypeError, AttributeError):
        pass
    return False


def _until_daily_reset() -> float:
    try:
        now = datetime.now(ZoneInfo('America/Los_Angeles'))
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo)
        return max(1.0, (tomorrow.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds() + 5)
    except Exception:
        return 86400.0


def _usage(body: dict[str, Any]) -> dict[str, int]:
    source = body.get('usageMetadata', {})
    if not isinstance(source, dict):
        return {}
    names = {
        'promptTokenCount': 'prompt_tokens', 'candidatesTokenCount': 'completion_tokens',
        'totalTokenCount': 'total_tokens', 'thoughtsTokenCount': 'thought_tokens',
        'cachedContentTokenCount': 'cached_tokens',
    }
    return {target: source[name] for name, target in names.items()
            if type(source.get(name)) is int and source[name] >= 0}


def generate_json(system_prompt: str, user_payload: str, marker: str,
                  max_output_tokens: int | None = None) -> dict[str, Any]:
    if not settings.GEMINI_API_KEY:
        raise GeminiError('El proveedor de análisis no está configurado.')
    model = settings.GEMINI_MODEL.removeprefix('models/')
    if not re.fullmatch(r'gemini-[A-Za-z0-9._-]+', model):
        raise GeminiError('El modelo de análisis configurado no es válido.')
    budget = min(max_output_tokens or settings.GEMINI_MAX_OUTPUT_TOKENS, settings.GEMINI_MAX_OUTPUT_TOKENS)
    config: dict[str, Any] = {
        'temperature': 0, 'maxOutputTokens': budget, 'responseMimeType': 'application/json',
    }
    cv_schema = cv_response_schema(user_payload)
    if cv_schema is not None:
        config['responseJsonSchema'] = cv_schema
    if model.startswith('gemini-3') and 'flash-lite' in model:
        config['thinkingConfig'] = {'thinkingLevel': 'minimal'}
    elif model.startswith('gemini-2.5'):
        config['thinkingConfig'] = {'thinkingBudget': 0}
    limiter = gemini_rate_limiter(model)
    estimate = estimate_input_tokens(system_prompt + user_payload)
    if estimate > min(settings.GEMINI_MAX_INPUT_TOKENS_PER_REQUEST, settings.GEMINI_INPUT_TOKENS_PER_MINUTE):
        raise ProviderResponseTruncated('El fragmento requiere dividirse para respetar el presupuesto de entrada.')
    limiter.reserve(marker, estimate)
    try:
        response = httpx.post(
            f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
            headers={'x-goog-api-key': settings.GEMINI_API_KEY, 'Content-Type': 'application/json'},
            json={
                'systemInstruction': {'parts': [{'text': system_prompt}]},
                'contents': [{'role': 'user', 'parts': [{'text': user_payload}]}],
                'generationConfig': config,
            },
            timeout=httpx.Timeout(90, connect=10),
        )
    except (httpx.HTTPError, OSError):
        wait = limiter.defer(30)
        logger.warning('Gemini request deferred reason=transport seconds=%.3f', wait)
        raise GeminiUnavailable(wait) from None
    if response.status_code in {500, 502, 503, 504}:
        wait = limiter.defer(_retry_delay(response, default=30))
        logger.warning('Gemini request deferred status=%d seconds=%.3f', response.status_code, wait)
        raise GeminiUnavailable(wait) from None
    if response.status_code == 429:
        delay = _retry_delay(response)
        daily_quota = _daily_quota_exhausted(response)
        if daily_quota:
            delay = max(delay, _until_daily_reset())
        wait = limiter.record_response({'Retry-After': str(delay)}, status_code=429, estimated_tokens=estimate)
        if delay > 3600:
            # The generic Retry-After parser bounds short provider waits to one
            # hour. Preserve a longer explicit RetryInfo for all Gemini workers.
            now = int(time.time() * 1000)
            limiter.client.eval(LUA_RECORD_RESPONSE, 2, limiter.cooldown_key, limiter.quota_key,
                                now, math.ceil(delay * 1000), '', '')
        logger.info('Gemini request deferred status=429 seconds=%.3f', max(delay, wait))
        raise RateLimitExceeded(max(delay, wait), reason='daily_budget' if daily_quota else 'provider') from None
    if response.status_code >= 400:
        raise GeminiError(f'El proveedor rechazó la solicitud de análisis (HTTP {response.status_code}).')
    limiter.record_response(response.headers, status_code=response.status_code, estimated_tokens=estimate)
    try:
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError
        candidates = body.get('candidates')
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            raise ValueError
        candidate = candidates[0]
        finish = candidate.get('finishReason')
        if finish == 'MAX_TOKENS':
            raise ProviderResponseTruncated('La respuesta del análisis quedó incompleta por el límite de tokens.')
        if finish != 'STOP':
            raise ValueError
        parts = candidate.get('content', {}).get('parts', [])
        if not isinstance(parts, list):
            raise ValueError
        text = ''.join(part['text'] for part in parts
                       if isinstance(part, dict) and part.get('thought') is not True and isinstance(part.get('text'), str))
        content = json.loads(text, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        if not isinstance(content, dict):
            raise ValueError
    except ProviderResponseTruncated:
        raise
    except (ValueError, TypeError, AttributeError, KeyError):
        raise GeminiError('El proveedor devolvió una respuesta JSON inválida o incompleta.') from None
    usage = _usage(body)
    logger.info('Gemini request completed status=%d prompt_tokens=%s completion_tokens=%s total_tokens=%s',
                response.status_code, usage.get('prompt_tokens'), usage.get('completion_tokens'), usage.get('total_tokens'))
    returned_model = body.get('modelVersion')
    safe_model = returned_model if isinstance(returned_model, str) and re.fullmatch(r'[A-Za-z0-9._/-]{1,120}', returned_model) else model
    return {'content': content, 'model': safe_model, 'usage': usage}
