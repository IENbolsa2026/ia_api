import unittest
import json
import pickle
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.services.cv_pipeline import _call_cv_provider
from app.services.gemini_client import generate_json
from app.services.rate_limiter import ProviderInputLimitExceeded, RateLimitExceeded
from app.services.cv_packets import make_packet_identity, process_packetized_cv_text


class GeminiAdmissionTests(unittest.TestCase):
    def test_oversized_input_has_distinct_error_before_http_or_quota_reservation(self):
        with patch.object(settings, 'GEMINI_API_KEY', 'synthetic-key'), \
             patch.object(settings, 'GEMINI_MAX_INPUT_TOKENS_PER_REQUEST', 1000), \
             patch('app.services.gemini_client.httpx.post') as post, \
             patch('app.services.gemini_client.gemini_rate_limiter') as limiter:
            with self.assertRaises(ProviderInputLimitExceeded) as raised:
                generate_json('s' * 1000, 'u' * 2000, 'synthetic')
        post.assert_not_called()
        limiter.assert_not_called()
        error = pickle.loads(pickle.dumps(raised.exception))
        self.assertEqual(error.limit_tokens, 1000)
        self.assertIn('entrada', str(error))
        self.assertNotIn('salida', str(error))


class GeminiPacketInputTests(unittest.IsolatedAsyncioTestCase):
    async def run_packets(self, provider):
        text = 'Texto sintético sin estudios. ' * 30
        identity = make_packet_identity(
            job_id='input-budget', analysis_id=1, candidate_id=42, cv_version_id=1,
            source_hash='a' * 64, text=text, prompt_version='synthetic',
            provider='gemini', model='synthetic', packet_chars=2000, block_chars=2000, budget=16384,
        )
        snapshots = []
        with patch('app.services.cv_packets.load_prompt', return_value='system'), \
             patch('app.services.cv_packets.build_cv_skill_context', new_callable=AsyncMock, return_value=''):
            result = await process_packetized_cv_text(
                text, 42, identity=identity, checkpoint=None, on_checkpoint=snapshots.append,
                on_heartbeat=None, call_provider=provider,
            )
        return result, snapshots

    async def test_fixed_prompt_over_budget_fails_once_without_recursive_splitting(self):
        provider = AsyncMock(side_effect=ProviderInputLimitExceeded(9000, 1000))
        with self.assertRaises(ProviderInputLimitExceeded):
            await self.run_packets(provider)
        provider.assert_awaited_once()

    async def test_text_over_budget_splits_and_completes_child_packets(self):
        calls = []

        async def provider(_system, payload, marker, **kwargs):
            request = json.loads(payload)
            calls.append(marker)
            if 'text' in request:
                if len(request['text']) > 500:
                    raise ProviderInputLimitExceeded(11000, 10000)
                return {}
            return {alias: [] for alias in ('sk', 'x', 'ed', 'l', 'c')}

        (profile, _model), snapshots = await self.run_packets(provider)
        self.assertEqual(profile['education'], [])
        self.assertEqual(len(calls), 4)  # parent + two leaves + consolidation
        self.assertEqual(snapshots[-1]['consolidation']['status'], 'completed')


class GeminiDailyDeferralTests(unittest.IsolatedAsyncioTestCase):
    async def test_efficient_short_quota_wait_yields_worker_instead_of_sleeping(self):
        error = RateLimitExceeded(45, reason='token_headroom')
        with patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock, side_effect=error) as call, \
             patch('app.services.cv_pipeline.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(RateLimitExceeded):
                await _call_cv_provider('system', 'data', 'synthetic', defer_quota_waits=True)
        call.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_daily_budget_yields_worker_without_sleeping_or_calling_again(self):
        error = RateLimitExceeded(86400, reason='daily_budget')
        with patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock, side_effect=error) as call, \
             patch('app.services.cv_pipeline.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(RateLimitExceeded) as raised:
                await _call_cv_provider('system', 'data', 'synthetic')
        self.assertIs(raised.exception, error)
        self.assertTrue(error.is_local_wait)
        call.assert_awaited_once()
        sleep.assert_not_awaited()
