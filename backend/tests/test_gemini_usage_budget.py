"""Execute quota Lua against an in-memory Redis; no network or provider calls.

Optional test dependency: pip install 'fakeredis[lua]'.
"""

import unittest

import pytest

fakeredis = pytest.importorskip('fakeredis')
pytest.importorskip('lupa')

from app.services.gemini_budget import GeminiBudgetLimiter
from app.services.rate_limiter import RateLimitExceeded


class GeminiUsageBudgetTests(unittest.TestCase):
    def setUp(self):
        self.client = fakeredis.FakeRedis(decode_responses=True)

    def budget(self):
        return GeminiBudgetLimiter(self.client, 'synthetic', rpm=4, tpm=20000, rpd=20, max_input_tokens=20000)

    def test_real_usage_releases_only_unused_tokens_without_resetting_requests_or_windows(self):
        first, second = self.budget(), self.budget()
        first.reserve('private marker never stored', 16000)
        original_time = self.client.zscore(first.minute_key, first._reservation_id)
        with self.assertRaises(RateLimitExceeded) as raised:
            second.reserve('second', 10000)
        self.assertEqual(raised.exception.reason, 'token_headroom')
        self.assertEqual(self.client.zcard(first.daily_key), 1)
        first.record_usage(6000)
        second.reserve('second', 10000)
        self.assertEqual(self.client.zcard(first.minute_key), 2)
        self.assertEqual(self.client.zcard(first.daily_key), 2)
        self.assertEqual(self.client.zscore(first.minute_key, first._reservation_id), original_time)
        self.assertEqual(self.client.hget(first.tokens_key, first._reservation_id), '6000')

    def test_underestimate_increases_reservation_and_blocks_next_request(self):
        first, second = self.budget(), self.budget()
        first.reserve('first', 10000)
        first.record_usage(19000)
        with self.assertRaises(RateLimitExceeded):
            second.reserve('second', 2000)

    def test_reconciliation_cannot_change_another_request_or_resurrect_an_expired_one(self):
        first, second = self.budget(), self.budget()
        first.reserve('first', 10000)
        second.reserve('second', 8000)
        first.record_usage(4000)
        self.assertEqual(self.client.hget(first.tokens_key, second._reservation_id), '8000')
        self.client.zrem(first.minute_key, first._reservation_id)
        first.record_usage(2000)
        self.assertIsNone(self.client.zscore(first.minute_key, first._reservation_id))
        self.assertEqual(self.client.hget(first.tokens_key, first._reservation_id), '4000')

    def test_missing_invalid_or_unadmitted_usage_never_frees_budget(self):
        first, second = self.budget(), self.budget()
        second.record_usage(1000)
        first.reserve('first', 18000)
        for value in (None, 0, -1, True, '1000'):
            first.record_usage(value)
        with self.assertRaises(RateLimitExceeded):
            second.reserve('second', 3000)
        self.assertEqual(self.client.zcard(first.daily_key), 1)
