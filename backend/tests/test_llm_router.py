"""Failover contracts use synthetic keys; no provider/network credentials needed."""

import json
import pickle
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import redis

from app.core.config import Settings, settings
from app.services import llm_router as router
from app.services.cv_pipeline import _call_provider
from app.services.match_groq import GroqClient
from app.services.rate_limiter import ProviderInputLimitExceeded, ProviderResponseTruncated, RateLimitExceeded


@pytest.fixture
def setup_router(monkeypatch):
    values = {
        'LLM_PROVIDER': 'auto', 'MATCH_PROVIDER': 'auto',
        'GEMINI_API_KEYS': ['gemini-1', 'gemini-2'], 'GEMINI_API_KEY': '',
        'GROQ_API_KEYS': ['groq-1'], 'GROQ_API_KEY': '',
        'AI_GATEWAY_API_KEYS': ['vercel-1', 'vercel-2', 'vercel-3', 'vercel-4'],
        'AI_GATEWAY_API_KEY': '', 'AI_GATEWAY_MODELS': ['google/gemini-2.5-flash-lite', 'openai/gpt-4o-mini'],
        'AI_GATEWAY_ENABLED': True,
        'GEMINI_FALLBACK_MODELS': [], 'GROQ_FALLBACK_MODELS': [],
        'LLM_FALLBACK_ORDER': ['gemini', 'vercel', 'groq'],
        'LLM_ROUTER_TIMEOUT_SECONDS': 90, 'LLM_ATTEMPT_TIMEOUT_SECONDS': 30,
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    limiters = {}

    def limiter(route, *, credential=False):
        scope = route.credential_scope if credential else route.scope
        if scope not in limiters:
            mock = MagicMock()
            mock.defer.side_effect = lambda seconds: seconds
            mock.record_response.return_value = 17
            limiters[scope] = mock
        return limiters[scope]

    with patch.object(router, '_limiter', side_effect=limiter), \
         patch.object(router, 'GeminiBudgetLimiter') as budget, \
         patch.object(router, 'gemini_json') as gemini, \
         patch.object(router.httpx, 'post') as post:
        gemini.side_effect = router.GeminiUnavailable(30)
        post.return_value = reply()
        yield gemini, post, budget, limiter


def reply(status=200, *, content=None, finish='stop', **extra):
    return httpx.Response(status, json={
        'model': 'actual-model', 'choices': [{'finish_reason': finish, 'message': {
            'content': json.dumps({'ok': True} if content is None else content),
        }}], 'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15}, **extra,
    })


def test_gemini_outage_uses_second_native_key_then_gateway(setup_router):
    gemini, post, _, _ = setup_router
    result = router.generate_json('system', 'source', 'marker', 950)
    assert [call.kwargs['api_key'] for call in gemini.call_args_list] == ['gemini-1', 'gemini-2']
    assert post.call_args.args[0] == 'https://ai-gateway.vercel.sh/v1/chat/completions'
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer vercel-1'
    assert post.call_args.kwargs['json']['max_tokens'] == 950
    assert result == {'content': {'ok': True}, 'model': 'actual-model',
                      'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15}}


def test_native_second_key_can_succeed_without_gateway(setup_router):
    gemini, post, _, _ = setup_router
    gemini.side_effect = [RateLimitExceeded(60), {'content': {'ok': True}, 'model': 'gemini', 'usage': {}}]
    assert router.generate_json('system', 'source', 'marker')['model'] == 'gemini'
    post.assert_not_called()


def test_four_gateway_keys_then_next_model_then_groq(setup_router):
    _, post, _, _ = setup_router
    post.side_effect = [reply(429)] * 8 + [reply()]
    router.generate_json('system', 'source', 'marker')
    calls = post.call_args_list
    assert [c.kwargs['headers']['Authorization'] for c in calls[:8]] == [f'Bearer vercel-{n}' for n in range(1, 5)] * 2
    assert [c.kwargs['json']['model'] for c in calls[:8]] == ['google/gemini-2.5-flash-lite'] * 4 + ['openai/gpt-4o-mini'] * 4
    assert calls[8].args[0] == 'https://api.groq.com/openai/v1/chat/completions'


def test_gateway_model_fallback_can_succeed(setup_router):
    _, post, _, _ = setup_router
    post.side_effect = [reply(503)] * 4 + [reply()]
    router.generate_json('system', 'source', 'marker', mode='vercel')
    assert post.call_count == 5
    assert post.call_args.kwargs['json']['model'] == 'openai/gpt-4o-mini'


def test_groq_output_capacity_rejection_changes_model_instead_of_waiting(setup_router, monkeypatch):
    _, post, _, get_limiter = setup_router
    monkeypatch.setattr(settings, 'LLM_FALLBACK_ORDER', ['groq'])
    monkeypatch.setattr(settings, 'GROQ_FALLBACK_MODELS', ['openai/gpt-oss-120b'])
    first = router.routes('auto')[0]
    post.side_effect = [httpx.Response(429, json={'error': {'message':
        'Request too large: expected output tokens exceed output tokens per minute (OTPM). Limit 1000, Requested 3338'}}), reply()]
    result = router.generate_json('system', 'source', 'marker')
    assert result['content'] == {'ok': True}
    assert post.call_count == 2
    assert post.call_args.kwargs['json']['model'] == 'openai/gpt-oss-120b'
    assert post.call_args.kwargs['json']['reasoning_effort'] == 'low'
    get_limiter(first).defer.assert_called_once_with(300)


def test_only_groq_output_capacity_is_a_specific_terminal_error(setup_router, monkeypatch):
    monkeypatch.setattr(settings, 'LLM_FALLBACK_ORDER', ['groq'])
    _, post, _, _ = setup_router
    post.return_value = httpx.Response(429, json={'error': {'message':
        'Request too large: output tokens per minute (OTPM), Limit: 1000 Requested: 3338'}})
    with pytest.raises(router.ProviderOutputLimitExceeded) as raised:
        router.generate_json('system', 'source', 'marker')
    assert raised.value.limit_num == 1000
    assert post.call_count == 1


@pytest.mark.parametrize('status', [401, 402, 403])
def test_disabled_key_is_skipped_for_other_models_and_next_call(setup_router, status):
    _, post, _, get_limiter = setup_router
    key = router.routes('vercel')[0]
    guard = get_limiter(key, credential=True)

    def pause(seconds):
        guard.reserve.side_effect = RateLimitExceeded(seconds, reason='cooldown')
        return seconds

    guard.defer.side_effect = pause
    post.side_effect = [reply(status), reply(503), reply(503), reply(503), reply()]
    router.generate_json('system', 'source', 'marker', mode='vercel')
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer vercel-2'
    post.reset_mock(side_effect=True)
    post.return_value = reply()
    router.generate_json('system', 'source', 'next-marker', mode='vercel')
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer vercel-2'


def test_local_cooldown_skips_http_and_uses_other_key(setup_router):
    _, post, _, get_limiter = setup_router
    route = router.routes('vercel')[0]
    get_limiter(route).reserve.side_effect = RateLimitExceeded(12, reason='cooldown')
    router.generate_json('system', 'source', 'marker', mode='vercel')
    assert post.call_count == 1
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer vercel-2'


def test_all_quotas_wait_for_shortest_delay_without_spinning(setup_router):
    gemini, post, _, _ = setup_router
    gemini.side_effect = RateLimitExceeded(70)
    post.return_value = reply(429)
    with pytest.raises(RateLimitExceeded) as raised:
        router.generate_json('system', 'source', 'marker')
    assert raised.value.is_local_wait
    assert 15 < raised.value.retry_after_seconds <= 17
    assert post.call_count == 9


def test_all_outages_use_existing_celery_policy_and_serializable_error(setup_router):
    _, post, _, _ = setup_router
    post.side_effect = httpx.ConnectError('synthetic secret')
    with pytest.raises(router.ProvidersUnavailable) as raised:
        router.generate_json('system', 'source', 'marker')
    assert isinstance(pickle.loads(pickle.dumps(raised.value)), router.GeminiUnavailable)
    assert 'synthetic' not in str(raised.value)
    assert post.call_count == 9


def test_primary_outage_does_not_delay_a_healthy_fallback_cooldown(setup_router, monkeypatch):
    gemini, post, _, get_limiter = setup_router
    monkeypatch.setattr(settings, 'AI_GATEWAY_ENABLED', False)
    gemini.side_effect = router.GeminiUnavailable(30)
    route = next(r for r in router.routes('auto') if r.provider == 'groq')
    get_limiter(route).reserve.side_effect = RateLimitExceeded(4, reason='token_headroom')
    with pytest.raises(RateLimitExceeded) as raised:
        router.generate_json('system', 'source', 'marker')
    assert raised.value.is_local_wait
    assert 3 < raised.value.retry_after_seconds <= 4
    post.assert_not_called()


def test_time_budget_stops_remaining_attempts(setup_router):
    gemini, post, _, _ = setup_router
    with patch.object(router.time, 'monotonic', side_effect=[0, 0, 1, 91, 91]):
        with pytest.raises(router.ProvidersUnavailable):
            router.generate_json('system', 'source', 'marker')
    assert gemini.call_count == 1
    post.assert_not_called()


@pytest.mark.parametrize('response,error', [
    (reply(400), RuntimeError), (reply(content=[]), RuntimeError),
    (reply(finish='length'), ProviderResponseTruncated), (reply(finish='content_filter'), RuntimeError),
    (httpx.Response(200, text='private CV'), RuntimeError),
])
def test_permanent_or_invalid_response_does_not_rotate(response, error, setup_router):
    _, post, _, _ = setup_router
    post.return_value = response
    with pytest.raises(error):
        router.generate_json('system', 'source', 'marker', mode='vercel')
    assert post.call_count == 1


def test_input_too_large_for_gemini_can_use_gateway(setup_router):
    gemini, _, _, _ = setup_router
    gemini.side_effect = ProviderInputLimitExceeded(9000, 1000)
    assert router.generate_json('system', 'source', 'marker')['content'] == {'ok': True}


def test_redis_failure_before_admission_never_sends_http(setup_router):
    _, post, _, get_limiter = setup_router
    route = router.routes('vercel')[0]
    get_limiter(route, credential=True).reserve.side_effect = redis.ConnectionError('synthetic')
    with pytest.raises(redis.RedisError):
        router.generate_json('system', 'source', 'marker', mode='vercel')
    post.assert_not_called()


def test_success_survives_bookkeeping_failure_and_reconciles_total_tokens(setup_router):
    _, post, budget, get_limiter = setup_router
    get_limiter(router.routes('vercel')[0]).record_response.side_effect = redis.ConnectionError('synthetic')
    budget.return_value.record_usage.side_effect = redis.ConnectionError('synthetic')
    assert router.generate_json('system', 'source', 'marker', mode='vercel')['content'] == {'ok': True}
    budget.return_value.record_usage.assert_called_once_with(15)
    assert post.call_count == 1


def test_cv_and_matching_route_and_return_actual_model(setup_router):
    with patch.object(router, 'generate_json', return_value={'content': {'ok': True}, 'model': 'fallback-model', 'usage': {}}) as call:
        assert _call_provider('system', 'source', 'marker')['__provider_model'] == 'fallback-model'
        response = GroqClient()._post({'messages': [{'content': 'system'}, {'content': 'source'}], 'max_tokens': 950})
        assert response.json()['model'] == 'fallback-model'
        assert call.call_args.kwargs['purpose'] == 'match'


def test_route_identity_keys_are_private_and_config_changes_invalidate_checkpoints(setup_router, monkeypatch):
    route = router.routes('vercel')[0]
    assert route.key not in repr(route)
    original = router.configured_model('auto')
    monkeypatch.setattr(settings, 'AI_GATEWAY_MODELS', ['openai/gpt-4o-mini'])
    assert router.configured_model('auto') != original
    assert 'vercel-1' not in route.scope + route.credential_scope


def test_env_arrays_support_four_keys_and_reject_invalid_provider(monkeypatch):
    monkeypatch.setenv('AI_GATEWAY_API_KEYS', '["a","b","c","d"]')
    parsed = Settings(_env_file=None)
    assert parsed.AI_GATEWAY_API_KEYS == ['a', 'b', 'c', 'd']
    monkeypatch.setenv('LLM_FALLBACK_ORDER', '["invalid"]')
    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_native_gemini_explicit_key_and_model_have_independent_scopes():
    from app.services.gemini_client import gemini_rate_limiter
    with patch('app.services.gemini_client.redis.from_url'):
        first = gemini_rate_limiter('gemini-2.5-flash', 'synthetic-a')
        second = gemini_rate_limiter('gemini-2.5-flash', 'synthetic-b')
    assert first.cooldown_key != second.cooldown_key


def test_gateway_toggle_off_skips_saved_keys_and_uses_groq(setup_router, monkeypatch):
    _, post, _, _ = setup_router
    monkeypatch.setattr(settings, 'AI_GATEWAY_ENABLED', False)
    assert {route.provider for route in router.routes('auto')} == {'gemini', 'groq'}
    router.generate_json('system', 'source', 'marker')
    assert post.call_count == 1
    assert post.call_args.args[0] == 'https://api.groq.com/openai/v1/chat/completions'


def test_gateway_toggle_off_blocks_explicit_vercel_mode_before_network(setup_router, monkeypatch):
    gemini, post, _, _ = setup_router
    monkeypatch.setattr(settings, 'AI_GATEWAY_ENABLED', False)
    with pytest.raises(ValueError, match='AI_GATEWAY_ENABLED'):
        router.generate_json('system', 'source', 'marker', mode='vercel')
    gemini.assert_not_called()
    post.assert_not_called()


def test_gateway_toggle_can_reenable_existing_keys(setup_router, monkeypatch):
    monkeypatch.setattr(settings, 'AI_GATEWAY_ENABLED', False)
    disabled_identity = router.configured_model('auto')
    monkeypatch.setattr(settings, 'AI_GATEWAY_ENABLED', True)
    assert len(router.routes('vercel')) == 8
    assert router.configured_model('auto') != disabled_identity


def test_gateway_is_disabled_by_default_and_env_toggle_parses(monkeypatch):
    monkeypatch.delenv('AI_GATEWAY_ENABLED', raising=False)
    assert Settings(_env_file=None).AI_GATEWAY_ENABLED is False
    monkeypatch.setenv('AI_GATEWAY_ENABLED', 'true')
    assert Settings(_env_file=None).AI_GATEWAY_ENABLED is True


def test_native_gemini_override_does_not_mutate_global_settings(monkeypatch):
    from app.services.gemini_client import generate_json
    monkeypatch.setattr(settings, 'GEMINI_API_KEY', 'original-key')
    monkeypatch.setattr(settings, 'GEMINI_MODEL', 'gemini-original')
    body = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '{"ok":true}'}]}}]}
    with patch('app.services.gemini_client.gemini_rate_limiter') as limiter, \
         patch('app.services.gemini_client.httpx.post', return_value=httpx.Response(200, json=body)) as post:
        generate_json('system', 'payload', 'marker', api_key='override-key', model='gemini-2.5-flash', timeout_seconds=4)
    limiter.assert_called_once_with('gemini-2.5-flash', 'override-key')
    assert post.call_args.kwargs['headers']['x-goog-api-key'] == 'override-key'
    assert post.call_args.kwargs['timeout'].read == 4
    assert (settings.GEMINI_API_KEY, settings.GEMINI_MODEL) == ('original-key', 'gemini-original')


def test_native_rejected_credentials_and_missing_model_allow_fallback(setup_router):
    gemini, post, _, _ = setup_router
    gemini.side_effect = [router.GeminiRejected(403), router.GeminiRejected(404)]
    assert router.generate_json('system', 'source', 'marker')['content'] == {'ok': True}
    assert post.call_count == 1


def test_gateway_daily_quota_applies_to_all_models_for_key(setup_router):
    _, post, quota, _ = setup_router
    quota.return_value.reserve.side_effect = RateLimitExceeded(86400, reason='daily_budget')
    with pytest.raises(RateLimitExceeded) as raised:
        router.generate_json('system', 'source', 'marker', mode='vercel')
    assert raised.value.is_local_wait
    assert raised.value.retry_after_seconds > 86000
    post.assert_not_called()


def test_secret_provider_errors_never_reach_logs_or_exceptions(setup_router, caplog):
    _, post, _, _ = setup_router
    post.return_value = httpx.Response(400, json={'error': {'message': 'PRIVATE_CV_AND_SECRET_KEY'}})
    with pytest.raises(RuntimeError) as raised:
        router.generate_json('system', 'PRIVATE_CV_AND_SECRET_KEY', 'marker', mode='vercel')
    assert 'PRIVATE_CV_AND_SECRET_KEY' not in str(raised.value) + caplog.text


def test_auto_cv_fallback_preserves_grounded_facts_and_resumable_checkpoint(setup_router):
    from app.services.cv_pipeline import process_cv_text
    from app.services.cv_packets import validate_manifest
    _, post, _, _ = setup_router
    text = 'Uso Python para desarrollar aplicaciones.'
    post.return_value = reply(content={'s': 'Resumen generado demasiado extenso. ' * 30, 'sk': [{
        'n': 'Python', 'k': 'technical', 'o': 'declared', 'l': 'beginner',
        'e': ['Desarrolla aplicaciones con Python.'], 'q': ['Uso Python para desarrollar aplicaciones.'],
    }]})
    checkpoints = []
    with patch('app.services.cv_packets.build_cv_skill_context', new_callable=AsyncMock, return_value=''):
        profile = asyncio.run(process_cv_text(text, 42, on_checkpoint=checkpoints.append))
    assert profile['skills'][0]['name'] == 'Python'
    assert profile['skills'][0]['source_quotes'] == [text]
    assert profile.provider_model == 'actual-model'
    assert post.call_count == 1
    sent = json.loads(post.call_args.kwargs['json']['messages'][1]['content'])
    assert 'source_text' in sent and 'fragment_schema' not in sent
    checkpoint = checkpoints[-1]
    assert checkpoint['consolidation']['status'] == 'completed'
    validate_manifest(checkpoint, text, checkpoint['identity'])


def test_413_rotates_to_next_model_without_retrying_same_request(setup_router, monkeypatch):
    _, post, _, _ = setup_router
    monkeypatch.setattr(settings, 'LLM_FALLBACK_ORDER', ['groq'])
    monkeypatch.setattr(settings, 'GROQ_FALLBACK_MODELS', ['openai/gpt-oss-120b'])
    post.side_effect = [reply(413, error={'message': 'Request too large. Limit 7000, Requested 8313'}), reply()]
    router.generate_json('system', 'source', 'match-provider', purpose='match')
    assert post.call_count == 2
    assert post.call_args.kwargs['json']['model'] == 'openai/gpt-oss-120b'


def test_match_can_adapt_413_even_when_gemini_is_unavailable(setup_router, monkeypatch):
    _, post, _, _ = setup_router
    monkeypatch.setattr(settings, 'LLM_FALLBACK_ORDER', ['gemini', 'groq'])
    post.return_value = reply(413, error={'message': 'Request too large. Limit 7000, Requested 8313'})
    with pytest.raises(ProviderInputLimitExceeded) as caught:
        router.generate_json('system', 'source', 'match-provider', purpose='match')
    assert (caught.value.estimated_tokens, caught.value.limit_tokens) == (8313, 7000)
