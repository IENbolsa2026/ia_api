"""Provider scheduling regression tests; all requests and Redis clients are fake."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from celery.exceptions import Retry

from app.core.config import settings
from app.services.cv_pipeline import _call_cv_provider, _call_groq
from app.services.match_groq import GroqClient
from app.services.rate_limiter import (
    RateLimitExceeded, RedisRateLimiter, groq_rate_limiter, reset_duration_seconds,
)
from app.tasks.cv_analysis_tasks import run_extraction_task
from app.tasks.match_tasks import evaluate_match_task


class ProviderPacingTests(unittest.TestCase):
    def test_documented_reset_durations_and_invalid_values(self):
        for raw, expected in [('7.66s', 7.66), ('2m59.56s', 179.56), ('1h2m3s', 3723), ('500ms', .5)]:
            with self.subTest(raw=raw):
                self.assertAlmostEqual(reset_duration_seconds(raw), expected)
        for raw in ('', '-1s', 'secret text', 'nan', 'inf', '7.66', None):
            self.assertIsNone(reset_duration_seconds(raw))

    def test_post_success_pause_starts_at_response_completion(self):
        client = MagicMock()
        client.eval.return_value = 12000
        limiter = RedisRateLimiter(client, 20, minimum_interval_seconds=12)
        with patch('app.services.rate_limiter.time.time', return_value=130):
            self.assertEqual(limiter.record_response({}, status_code=200), 12)
        # Even if inference started at t=100, pacing starts after t=130.
        self.assertEqual(client.eval.call_args.args[4:6], (130000, 12000))

    def test_429_uses_token_reset_but_does_not_treat_daily_requests_as_rpm(self):
        client = MagicMock()
        client.eval.return_value = 45000
        limiter = RedisRateLimiter(client, 20, minimum_interval_seconds=12)
        wait = limiter.record_response({
            'Retry-After': '2', 'x-ratelimit-remaining-tokens': '100',
            'x-ratelimit-reset-tokens': '45s', 'x-ratelimit-remaining-requests': '900',
            'x-ratelimit-reset-requests': '2h',
        }, status_code=429, estimated_tokens=3000)
        self.assertEqual(wait, 45)
        self.assertEqual(client.eval.call_args.args[5], 45000)

    def test_success_preserves_token_headroom_for_next_call_and_logs_no_private_headers(self):
        client = MagicMock()
        client.eval.return_value = 12000
        limiter = RedisRateLimiter(client, 20, minimum_interval_seconds=12)
        with patch('app.services.rate_limiter.time.time', return_value=100), \
             self.assertLogs('app.services.rate_limiter', level='INFO') as logs:
            limiter.record_response({
                'x-ratelimit-remaining-tokens': '400', 'x-ratelimit-reset-tokens': '1m2.5s',
                'authorization': 'private credential sentinel', 'x-unknown': 'private CV sentinel',
            }, status_code=200)
        self.assertEqual(client.eval.call_args.args[-2:], (400, 162500))
        self.assertNotIn('sentinel', str(logs.output))

    def test_local_cooldown_and_headroom_are_classified_separately_from_provider_errors(self):
        for code, reason in ((1, 'cooldown'), (2, 'token_headroom'), (3, 'rpm')):
            client = MagicMock()
            client.eval.return_value = [0, 12000, code]
            with self.assertRaises(RateLimitExceeded) as error:
                RedisRateLimiter(client, 20).reserve('synthetic', 3000)
            self.assertTrue(error.exception.is_local_wait)
            self.assertEqual(error.exception.reason, reason)
        self.assertFalse(RateLimitExceeded(12).is_local_wait)

    def test_cooldown_scope_separates_credentials_models_and_openai(self):
        with patch.object(settings, 'GROQ_API_KEY', 'synthetic-one'):
            first = groq_rate_limiter('synthetic-model')
            other_model = groq_rate_limiter('different-model')
        with patch.object(settings, 'GROQ_API_KEY', 'synthetic-two'):
            other_account = groq_rate_limiter('synthetic-model')
        self.assertNotEqual(first.cooldown_key, other_account.cooldown_key)
        self.assertNotEqual(first.cooldown_key, other_model.cooldown_key)
        self.assertNotIn('synthetic', first.cooldown_key)
        openai = RedisRateLimiter(MagicMock(), 20, provider='openai', cooldown_scope='openai')
        self.assertNotEqual(first.rate_key, openai.rate_key)
        self.assertNotEqual(first.cooldown_key, openai.cooldown_key)

    def test_cv_sdk_records_success_headers_without_changing_output_budget(self):
        limiter = MagicMock()
        response = SimpleNamespace(
            choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{}'))],
            model='synthetic-model',
        )
        raw = MagicMock(headers={'x-ratelimit-remaining-tokens': '1000'})
        raw.parse.return_value = response
        with patch.object(settings, 'GROQ_API_KEY', 'synthetic'), \
             patch('app.services.cv_pipeline.groq_rate_limiter', return_value=limiter), \
             patch('app.services.cv_pipeline.Groq') as sdk:
            sdk.return_value.chat.completions.with_raw_response.create.return_value = raw
            result = _call_groq('synthetic system', 'synthetic text', 'synthetic', max_output_tokens=950)
        self.assertEqual(result['__provider_model'], 'synthetic-model')
        self.assertEqual(sdk.return_value.chat.completions.with_raw_response.create.call_args.kwargs['max_tokens'], 950)
        limiter.record_response.assert_called_once()
        self.assertEqual(limiter.record_response.call_args.kwargs['status_code'], 200)

    def test_match_waits_before_http_and_reserves_rpm_once(self):
        limiter = MagicMock()
        limiter.reserve.side_effect = RateLimitExceeded(12, reason='cooldown')
        with patch('app.services.match_groq.groq_rate_limiter', return_value=limiter), \
             patch('app.services.match_groq.httpx.post') as post:
            with self.assertRaises(RateLimitExceeded):
                GroqClient()._post({'model': 'synthetic', 'messages': [{'content': 'system'}, {'content': 'user'}], 'max_tokens': 950})
        post.assert_not_called()
        limiter.reserve.assert_called_once()
        self.assertNotIn('count_request', limiter.reserve.call_args.kwargs)


class CvLocalWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_pacing_does_not_exhaust_five_provider_retries(self):
        waits = [RateLimitExceeded(12, reason='cooldown') for _ in range(8)]
        with patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock) as provider, \
             patch('app.services.cv_pipeline.asyncio.sleep', new_callable=AsyncMock):
            provider.side_effect = waits + [{'synthetic': True}]
            result = await _call_cv_provider('system', 'text', 'synthetic')
        self.assertEqual(result, {'synthetic': True})
        self.assertEqual(provider.await_count, 9)


class TaskPacingCounterTests(unittest.TestCase):
    def run_normalization_failure(self, state, error, retries):
        store = MagicMock()
        store.get.side_effect = lambda _job_id: state.copy()
        store.acquire_lock.return_value = True
        store.update_owned.side_effect = lambda _job_id, **changes: state.update(changes)
        with patch('app.tasks.cv_analysis_tasks.ExtractionJobStore', return_value=store), \
             patch('app.tasks.cv_analysis_tasks.normalize_candidate_profile', new_callable=AsyncMock, side_effect=error), \
             patch('app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay'), \
             patch.object(run_extraction_task, 'retry', side_effect=Retry('deferred')):
            run_extraction_task.push_request(retries=retries)
            try:
                return run_extraction_task.run('synthetic')
            finally:
                run_extraction_task.pop_request()

    def test_normalization_local_waits_then_first_429_do_not_consume_failure_budget(self):
        state = {'status': 'accepted', 'payload': {'kind': 'profile_normalization', 'candidate_id': 1, 'profile_snapshot': {}}}
        for attempt in range(7):
            with self.assertRaises(Retry):
                self.run_normalization_failure(state, RateLimitExceeded(12, reason='cooldown'), attempt)
        self.assertNotIn('provider_failures', state)
        with self.assertRaises(Retry):
            self.run_normalization_failure(state, RateLimitExceeded(45), 7)
        self.assertEqual(state['provider_failures'], 1)
        self.assertEqual(state['status'], 'rate_limited')
        for count in range(2, 5):
            with self.assertRaises(Retry):
                self.run_normalization_failure(state, RateLimitExceeded(45), 7 + count)
        terminal = self.run_normalization_failure(state, RateLimitExceeded(45), 12)
        self.assertEqual(terminal['status'], 'failed')
        self.assertEqual(state['provider_failures'], 5)

    def test_match_local_wait_then_first_429_is_not_terminal_and_fifth_is(self):
        for previous_failures, expected_terminal in ((0, False), (4, True)):
            state = {
                'status': 'accepted', 'request': {'candidate_profile': {}, 'job_offer': {}},
                'provider_failures': previous_failures,
            }
            store = MagicMock()
            store.get.side_effect = lambda _request_id: state.copy()
            store.acquire_lock.return_value = True
            store.update_owned.side_effect = lambda _request_id, **changes: state.update(changes)
            with patch('app.tasks.match_tasks.MatchJobStore', return_value=store), \
                 patch('app.tasks.match_tasks.GroqClient') as provider, \
                 patch.object(evaluate_match_task, 'retry', side_effect=Retry('deferred')):
                provider.return_value.evaluate.side_effect = RateLimitExceeded(45)
                evaluate_match_task.push_request(retries=50)
                try:
                    with self.assertRaises(RateLimitExceeded if expected_terminal else Retry):
                        evaluate_match_task.run('synthetic')
                finally:
                    evaluate_match_task.pop_request()
            self.assertEqual(state['provider_failures'], previous_failures + 1)
            self.assertEqual(state['status'], 'failed' if expected_terminal else 'rate_limited')
