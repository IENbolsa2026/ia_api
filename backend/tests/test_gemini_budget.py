from unittest import TestCase
from unittest.mock import Mock

from app.services.gemini_budget import (
    GeminiBudgetLimiter,
    LUA_RESERVE_GEMINI,
    estimate_input_tokens,
)
from app.services.rate_limiter import RateLimitExceeded


class GeminiBudgetLimiterTests(TestCase):
    def setUp(self):
        self.client = Mock()
        self.limiter = GeminiBudgetLimiter(
            self.client, 'gemini:test-scope', rpm=4, tpm=20000, rpd=20,
            max_input_tokens=12000,
        )

    def test_estimator_uses_ceil_half_characters(self):
        self.assertEqual(estimate_input_tokens('a' * 131), 66)
        self.assertEqual(estimate_input_tokens('a' * 2200), 1100)
        self.assertEqual(estimate_input_tokens(''), 0)

    def test_reservation_calls_atomic_script_with_shared_cooldown_scope(self):
        self.client.eval.return_value = [1, 0, 0]

        self.assertIsNone(self.limiter.reserve('opaque-marker', 1100))

        args = self.client.eval.call_args.args
        self.assertEqual(args[0], LUA_RESERVE_GEMINI)
        self.assertEqual(args[1], 4)
        self.assertEqual(args[2:6], (
            'ien-extraction:gemini-budget:gemini:test-scope:minute',
            'ien-extraction:gemini-budget:gemini:test-scope:daily',
            'ien-extraction:gemini-budget:gemini:test-scope:tokens',
            'ien-extraction:cooldown:gemini:test-scope',
        ))
        self.assertEqual(args[6:11], (4, 20000, 20, 12000, 1100))
        self.assertNotIn('opaque-marker', args)
        self.assertIn("redis.call('TIME')", LUA_RESERVE_GEMINI)

    def test_maps_all_budget_wait_reasons(self):
        for code, expected in (
            (1, 'rpm'),
            (2, 'token_headroom'),
            (3, 'daily_budget'),
            (4, 'cooldown'),
        ):
            with self.subTest(reason=expected):
                self.client.eval.return_value = [0, 1500, code]
                with self.assertRaises(RateLimitExceeded) as caught:
                    self.limiter.reserve('marker', 100)
                self.assertEqual(caught.exception.reason, expected)
                self.assertEqual(caught.exception.retry_after_seconds, 1.5)

    def test_oversized_single_request_fails_before_redis_reservation(self):
        with self.assertRaises(ValueError):
            self.limiter.reserve('marker', 12001)
        self.client.eval.assert_not_called()

    def test_nonpositive_wait_is_clamped(self):
        self.client.eval.return_value = [0, 0, 3]
        with self.assertRaises(RateLimitExceeded) as caught:
            self.limiter.reserve('marker', 1)
        self.assertEqual(caught.exception.retry_after_seconds, 0.001)
