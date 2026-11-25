"""Bounded failover across native providers and Vercel AI Gateway.

Redis shares admission/cooldowns between workers. Credentials never appear in
logs, exception messages or Redis keys. A candidate is attempted at most once
per call; invalid/truncated output stays with the existing validation pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import redis

from app.core.config import settings
from app.services.cv_request_budget import prepare_gemini_input
from app.services.gemini_budget import GeminiBudgetLimiter
from app.services.gemini_client import GeminiError, GeminiRejected, GeminiUnavailable, generate_json as gemini_json
from app.services.rate_limiter import (
    ProviderInputLimitExceeded, ProviderOutputLimitExceeded, ProviderResponseTruncated, RateLimitExceeded,
    RedisRateLimiter, estimate_request_tokens,
)

logger = logging.getLogger(__name__)


class ProvidersUnavailable(GeminiUnavailable):
    """Uses the existing Celery transient-outage retry policy."""

    provider_label = 'El servicio de IA'

    def __str__(self) -> str:
        return 'Los proveedores de IA no están disponibles temporalmente. El análisis se reintentará.'


@dataclass(frozen=True)
class Route:
    provider: str
    model: str
    key: str = field(repr=False)

    @property
    def scope(self) -> str:
        return f'{self.provider}:' + hashlib.sha256(f'{self.key}\0{self.model}'.encode()).hexdigest()[:32]

    @property
    def credential_scope(self) -> str:
        return f'{self.provider}:credential:' + hashlib.sha256(self.key.encode()).hexdigest()[:32]


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def routes(mode: str, purpose: str = 'cv') -> list[Route]:
    if mode not in {'auto', 'vercel'}:
        raise ValueError('El router requiere LLM_PROVIDER/MATCH_PROVIDER=auto o vercel.')
    if mode == 'vercel' and not settings.AI_GATEWAY_ENABLED:
        raise ValueError('Vercel AI Gateway está desactivado. Revisá AI_GATEWAY_ENABLED.')
    order = _unique(settings.LLM_FALLBACK_ORDER) if mode == 'auto' else ['vercel']
    if not order:
        raise ValueError('LLM_FALLBACK_ORDER no puede estar vacío.')
    credentials = {
        'gemini': _unique(settings.GEMINI_API_KEYS + [settings.GEMINI_API_KEY]),
        'groq': _unique(settings.GROQ_API_KEYS + [settings.GROQ_API_KEY]),
        'vercel': _unique(settings.AI_GATEWAY_API_KEYS + [settings.AI_GATEWAY_API_KEY]),
    }
    groq_model = (settings.MATCH_GROQ_MODEL or settings.CV_MODEL) if purpose == 'match' else settings.CV_MODEL
    models = {
        'gemini': _unique([settings.GEMINI_MODEL.removeprefix('models/')] + settings.GEMINI_FALLBACK_MODELS),
        'groq': _unique([groq_model] + settings.GROQ_FALLBACK_MODELS),
        'vercel': _unique(settings.AI_GATEWAY_MODELS),
    }
    candidates = []
    for provider in order:
        if provider == 'vercel' and not settings.AI_GATEWAY_ENABLED:
            continue
        if not credentials[provider]:
            continue
        if not models[provider]:
            raise ValueError('Configurá al menos un modelo para cada proveedor habilitado.')
        for model in models[provider]:
            pattern = r'gemini-[A-Za-z0-9._-]+' if provider == 'gemini' else r'[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+' if provider == 'vercel' else r'[A-Za-z0-9._/-]+'
            if not re.fullmatch(pattern, model):
                raise ValueError('Hay un identificador de modelo de IA inválido.')
            # Try the configured keys before moving to the next model.
            candidates.extend(Route(provider, model, key) for key in credentials[provider])
    if not candidates:
        raise ValueError('No hay claves de IA configuradas para el router.')
    return candidates


def configured_model(mode: str, purpose: str = 'cv') -> str:
    # Include routing configuration in checkpoint identity, without credentials.
    identities = list(dict.fromkeys(f'{r.provider}:{r.model}' for r in routes(mode, purpose)))
    return mode + ':' + hashlib.sha256(json.dumps(identities).encode()).hexdigest()[:24]


def _limiter(route: Route, *, credential: bool = False) -> RedisRateLimiter:
    limiter = RedisRateLimiter(
        redis.from_url(settings.JOB_STORE_REDIS_URL, decode_responses=True),
        settings.GROQ_REQUESTS_PER_MINUTE if route.provider == 'groq' else settings.AI_GATEWAY_REQUESTS_PER_MINUTE,
        provider=route.provider,
        cooldown_scope=route.credential_scope if credential else route.scope,
        minimum_interval_seconds=settings.GROQ_PAUSA_SEGUNDOS if route.provider == 'groq' and not credential else 0,
    )
    # The legacy limiter has one provider-wide RPM key. The router isolates it
    # per credential/model so exhausting one route cannot block every fallback.
    limiter.rate_key = f'ien-extraction:rate:{route.scope}'
    return limiter


def _record_success(limiter: RedisRateLimiter, response: httpx.Response, estimate: int) -> None:
    try:
        limiter.record_response(response.headers, status_code=200, estimated_tokens=estimate)
    except redis.RedisError:
        logger.warning('LLM response received; quota bookkeeping unavailable')


def _output_capacity_error(response: httpx.Response) -> ProviderOutputLimitExceeded | None:
    try:
        body = response.json()
        error = body.get('error', {}) if isinstance(body, dict) else {}
        message = str(error.get('message', '')).casefold() if isinstance(error, dict) else ''
    except ValueError:
        return None
    if ('request too large' in message and 'output tokens' in message
            and ('otpm' in message or ('expected output tokens' in message and 'exceed' in message))):
        limit = re.search(r'\blimit\s*[:=]?\s*(\d+)\b', message)
        return ProviderOutputLimitExceeded(int(limit.group(1)) if limit else None)
    return None


# Fields that only repeat the CV text literally. Gemini receives the full
# profile; capacity-limited fallbacks (Groq free tier: 8000 tokens/minute)
# receive the same facts without these duplicates. Array positions are kept,
# so every JSON Pointer the provider returns still resolves in the original
# profile, which the server validates against the full evidence map.
_MATCH_HEAVY_FIELDS = frozenset({'source_quotes'})


def _strip_heavy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_heavy(item) for key, item in value.items()
                if key not in _MATCH_HEAVY_FIELDS and item not in (None, '', [], {})}
    if isinstance(value, list):
        return [_strip_heavy(item) for item in value]
    return value


def compact_match_payload(payload: str) -> str:
    """Shrink a match batch for capacity-limited providers without changing facts."""
    try:
        data = json.loads(payload)
    except ValueError:
        return payload
    if not isinstance(data, dict) or not isinstance(data.get('candidate_profile'), dict):
        return payload
    data['candidate_profile'] = _strip_heavy(data['candidate_profile'])
    index = data.get('candidate_evidence_index')
    if isinstance(index, dict) and isinstance(index.get('paths'), list):
        index['paths'] = [path for path in index['paths']
                          if not any(f'/{field}' in str(path) for field in _MATCH_HEAVY_FIELDS)]
    elif isinstance(index, list):
        data['candidate_evidence_index'] = [path for path in index
                                            if not any(f'/{field}' in str(path) for field in _MATCH_HEAVY_FIELDS)]
    elif isinstance(index, dict):
        # The exact grouped index uses record prefixes and relative leaves.
        # Remove references to fields absent from this provider's transport.
        data['candidate_evidence_index'] = {
            prefix: [leaf for leaf in leaves
                     if not any(f'/{field}' in prefix + '/' + str(leaf) for field in _MATCH_HEAVY_FIELDS)]
            for prefix, leaves in index.items() if isinstance(leaves, list)
        }
    return json.dumps(data, ensure_ascii=False, separators=(',', ':'))


def _http_json(route: Route, system: str, payload: str, marker: str, output: int | None,
               timeout: float, credential: RedisRateLimiter) -> dict[str, Any]:
    limit = settings.AI_GATEWAY_MAX_OUTPUT_TOKENS if route.provider == 'vercel' else settings.GROQ_MAX_TOKENS
    budget = min(output or limit, limit)
    # Apply the same compact source/quote transport used by native Gemini.
    payload, schema, _ = prepare_gemini_input(system, payload)
    if marker == 'match-provider':
        payload = compact_match_payload(payload)
    if schema is not None:
        system += '\nRespondé con este esquema JSON: ' + json.dumps(schema, ensure_ascii=False, separators=(',', ':'))
    estimate = estimate_request_tokens(system, payload, budget)
    limiter = _limiter(route)
    quota = None
    if route.provider == 'vercel':
        # Credits and rate limits can be shared by every model for a key.
        quota = GeminiBudgetLimiter(limiter.client, route.credential_scope,
                                    settings.AI_GATEWAY_REQUESTS_PER_MINUTE,
                                    settings.AI_GATEWAY_TOKENS_PER_MINUTE,
                                    settings.AI_GATEWAY_REQUESTS_PER_DAY,
                                    settings.AI_GATEWAY_TOKENS_PER_MINUTE)
        if estimate > settings.AI_GATEWAY_TOKENS_PER_MINUTE:
            raise ProviderInputLimitExceeded(estimate, settings.AI_GATEWAY_TOKENS_PER_MINUTE)
        limiter.reserve(marker, estimate, count_request=False)
        quota.reserve(marker, estimate)
    else:
        limiter.reserve(marker, estimate)
    request: dict[str, Any] = {
        'model': route.model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': payload}],
        'temperature': 0, 'max_tokens': budget, 'response_format': {'type': 'json_object'},
    }
    if route.provider == 'groq' and route.model == 'qwen/qwen3.8-27b':
        request.update(reasoning_effort='low' if marker == 'match-provider' else 'none', reasoning_format='hidden')
    elif route.provider == 'groq' and route.model.startswith('openai/gpt-oss-'):
        request.update(reasoning_effort='low', reasoning_format='hidden')
    url = ('https://ai-gateway.vercel.sh/v1/chat/completions' if route.provider == 'vercel'
           else 'https://api.groq.com/openai/v1/chat/completions')
    try:
        response = httpx.post(url, headers={'Authorization': f'Bearer {route.key}'}, json=request,
                              timeout=httpx.Timeout(timeout, connect=min(10, timeout)))
    except (httpx.HTTPError, OSError):
        raise ProvidersUnavailable(limiter.defer(30)) from None
    if response.status_code == 413:
        # No sleep can make the same oversized request fit. Try another
        # route, then let the match client split its requirements if needed.
        requested, limit = estimate, max(1, estimate - 1)
        try:
            error = response.json().get('error', {})
            message = str(error.get('message', ''))
            found_limit = re.search(r'(?i)\blimit\s*[:=]?\s*(\d+)', message)
            found_requested = re.search(r'(?i)\brequested\s*[:=]?\s*(\d+)', message)
            if found_limit and int(found_limit[1]) > 0:
                limit = int(found_limit[1])
            if found_requested:
                requested = int(found_requested[1])
        except (ValueError, TypeError, AttributeError):
            pass
        logger.info('LLM route input capacity unavailable provider=%s requested=%d limit=%d',
                    route.provider, requested, limit)
        raise ProviderInputLimitExceeded(requested, limit)
    if response.status_code == 429:
        capacity_error = _output_capacity_error(response) if route.provider == 'groq' else None
        if capacity_error is not None:
            limiter.defer(300)
            logger.info('LLM route output capacity unavailable provider=groq limit=%s', capacity_error.limit_num)
            raise capacity_error
        raise RateLimitExceeded(limiter.record_response(response.headers, status_code=429, estimated_tokens=estimate))
    if response.status_code in {400, 422}:
        try:
            error = response.json().get('error', {})
            model_rejection = (error.get('code') in {
                'json_validate_failed', 'unsupported_parameter', 'unsupported_value',
                'unsupported_model', 'model_not_supported',
            } or 'failed_generation' in error)
        except (ValueError, AttributeError, TypeError):
            model_rejection = False
        if model_rejection:
            # Rotate model-specific failures without hiding a globally malformed
            # request. Never log the failed generation, which contains CV data.
            logger.info('LLM route rejected generation provider=%s status=%d', route.provider, response.status_code)
            raise ProvidersUnavailable(limiter.defer(30))
    if response.status_code in {401, 402, 403}:
        raise RateLimitExceeded(credential.defer(settings.LLM_CREDENTIAL_COOLDOWN_SECONDS), reason='cooldown')
    if response.status_code in {404, 408} or response.status_code >= 500:
        raise ProvidersUnavailable(limiter.defer(30))
    if response.status_code >= 400:
        raise RuntimeError(f'El proveedor rechazó la solicitud de IA (HTTP {response.status_code}).')
    _record_success(limiter, response, estimate)
    try:
        body = response.json()
        raw_usage = body.get('usage', {})
        usage = {name: value for name, value in (raw_usage.items() if isinstance(raw_usage, dict) else [])
                 if name in {'prompt_tokens', 'completion_tokens', 'total_tokens'} and type(value) is int and value >= 0}
        if quota is not None:
            try:
                quota.record_usage(usage.get('total_tokens'))
            except redis.RedisError:
                logger.warning('LLM response received; token reconciliation unavailable')
        choice = body['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise ProviderResponseTruncated('La respuesta de IA quedó incompleta por el límite de tokens.')
        if choice.get('finish_reason') != 'stop':
            raise ValueError
        content = json.loads(choice['message']['content'], parse_constant=lambda _v: (_ for _ in ()).throw(ValueError()))
        if not isinstance(content, dict):
            raise ValueError
    except ProviderResponseTruncated:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, IndexError):
        raise RuntimeError('El proveedor devolvió una respuesta JSON inválida o incompleta.') from None
    returned_model = body.get('model')
    model = returned_model if isinstance(returned_model, str) and re.fullmatch(r'[A-Za-z0-9._/-]{1,120}', returned_model) else route.model
    return {'content': content, 'model': model, 'usage': usage}


def generate_json(system: str, payload: str, marker: str, max_output_tokens: int | None = None,
                  *, mode: str = 'auto', purpose: str = 'cv') -> dict[str, Any]:
    deadline = time.monotonic() + settings.LLM_ROUTER_TIMEOUT_SECONDS
    waits: list[float] = []
    input_errors: list[ProviderInputLimitExceeded] = []
    output_errors: list[ProviderOutputLimitExceeded] = []
    outage = False
    quota_wait = False
    for route in routes(mode, purpose):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            outage = True
            break
        timeout = min(settings.LLM_ATTEMPT_TIMEOUT_SECONDS, remaining)
        credential = _limiter(route, credential=True)
        try:
            credential.reserve(marker, count_request=False)
            if route.provider == 'gemini':
                try:
                    return gemini_json(system, payload, marker, max_output_tokens,
                                       api_key=route.key, model=route.model, timeout_seconds=timeout)
                except GeminiRejected as exc:
                    if exc.status_code in {401, 403}:
                        raise RateLimitExceeded(credential.defer(settings.LLM_CREDENTIAL_COOLDOWN_SECONDS), reason='cooldown') from None
                    if exc.status_code == 404:
                        raise ProvidersUnavailable(_limiter(route).defer(30)) from None
                    raise
            return _http_json(route, system, payload, marker, max_output_tokens, timeout, credential)
        except ProviderInputLimitExceeded as exc:
            input_errors.append(exc)
            continue
        except ProviderOutputLimitExceeded as exc:
            output_errors.append(exc)
            continue
        except RateLimitExceeded as exc:
            quota_wait = True
            waits.append(time.monotonic() + exc.retry_after_seconds)
        except GeminiUnavailable as exc:
            outage = True
            waits.append(time.monotonic() + exc.retry_after_seconds)
        logger.info('LLM fallback provider=%s model=%s', route.provider, route.model)
    wait = max(1, min(waits) - time.monotonic()) if waits else 30
    if purpose == 'match' and input_errors:
        # Prefer adapting this batch over repeating a known-too-large request
        # while the primary provider is unavailable or cooling down.
        raise max(input_errors, key=lambda exc: exc.limit_tokens)
    if quota_wait:
        # A healthy fallback may become ready before the failed provider's
        # outage backoff. Yield until the earliest route is ready, without
        # counting the primary outage as failure of every provider.
        raise RateLimitExceeded(wait, reason='cooldown')
    if outage:
        raise ProvidersUnavailable(wait)
    if waits:
        # All remaining candidates are cooling down; do not burn task retries.
        raise RateLimitExceeded(wait, reason='cooldown')
    if input_errors:
        raise max(input_errors, key=lambda exc: exc.limit_tokens)
    if output_errors:
        raise output_errors[-1]
    raise GeminiError('No hay proveedores de IA disponibles.')
