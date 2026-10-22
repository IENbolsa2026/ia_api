import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import settings
from app.services.cv_pipeline import _call_cv_provider
from app.services.gemini_client import generate_json
from app.services.rate_limiter import ProviderResponseTruncated, RateLimitExceeded


class GeminiAdmissionTests(unittest.TestCase):
    def test_oversized_input_splits_before_http_or_quota_reservation(self):
        with patch.object(settings, 'GEMINI_API_KEY', 'synthetic-key'), \
             patch.object(settings, 'GEMINI_MAX_INPUT_TOKENS_PER_REQUEST', 1000), \
             patch('app.services.gemini_client.httpx.post') as post, \
             patch('app.services.gemini_client.gemini_rate_limiter') as limiter:
            with self.assertRaises(ProviderResponseTruncated):
                generate_json('s' * 1000, 'u' * 2000, 'synthetic')
        post.assert_not_called()
        limiter.return_value.reserve.assert_not_called()


class GeminiDailyDeferralTests(unittest.IsolatedAsyncioTestCase):
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
