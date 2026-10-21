import unittest
import json
from unittest.mock import patch

from app.core.config import settings
from app.services.match_groq import GroqClient
from app.services.rate_limiter import (
    ProviderOutputLimitExceeded,
    RateLimitExceeded,
    RedisRateLimiter,
    retry_after_seconds,
)
from app.services.cv_pipeline import _call_groq


class RateLimitErrorTests(unittest.TestCase):
    def setUp(self):
        limiter = patch('app.services.match_groq.groq_rate_limiter')
        self.match_limiter = limiter.start().return_value
        self.match_limiter.record_response.return_value = 45
        self.addCleanup(limiter.stop)

    def test_retry_after_accepts_seconds_and_bounds_extremes(self):
        self.assertEqual(retry_after_seconds("30"), 30)
        self.assertEqual(retry_after_seconds("7200"), 3600)
        self.assertEqual(retry_after_seconds("invalid"), 60)

    def test_repeated_reservations_with_same_marker_use_distinct_redis_members(self):
        class FakeRedis:
            def __init__(self):
                self.members = []

            def eval(self, *args):
                self.members.append(args[-1])
                return [1, 0]

        client = FakeRedis()
        limiter = RedisRateLimiter(client, 20)
        limiter.reserve("cv-candidate-1-fragment-1")
        limiter.reserve("cv-candidate-1-fragment-1")
        self.assertEqual(len(set(client.members)), 2)

    @patch("app.services.match_groq.httpx.post")
    def test_match_provider_429_uses_retry_after_without_exposing_body(self, post):
        response = type("Response", (), {"status_code": 429, "headers": {"Retry-After": "45"},
                                          "text": "private provider message"})()
        post.return_value = response
        profile = {"skills": [{"name": "Conducción profesional", "evidence": ["Opera camiones"]}]}
        with patch.object(settings, "GROQ_API_KEY", "configured"), self.assertRaises(RateLimitExceeded) as caught:
            GroqClient().evaluate_v2({"candidate_profile": profile, "job_offer": {"requirements": []},
                                      "evaluation_date": "2026-10-16",
                                      "candidate_evidence_index": {"/skills/0/name": "Conducción profesional",
                                                                    "/skills/0/evidence/0": "Opera camiones"}})
        self.assertEqual(caught.exception.retry_after_seconds, 45)
        self.assertNotIn("private provider message", str(caught.exception))
        user_payload = json.loads(post.call_args.kwargs["json"]["messages"][1]["content"])
        self.assertEqual(user_payload["candidate_profile"], profile)
        self.assertEqual(user_payload["candidate_evidence_index"], ["/skills/0/evidence/0", "/skills/0/name"])

    def _call_with_groq_error(self, provider_error):
        with patch.object(settings, "GROQ_API_KEY", "synthetic-key"), \
             patch("app.services.cv_pipeline.groq_rate_limiter") as limiter, \
             patch("app.services.cv_pipeline.redis.from_url"), \
             patch("app.services.cv_pipeline.Groq") as groq:
            limiter.return_value.record_response.return_value = 45
            groq.return_value.chat.completions.with_raw_response.create.side_effect = provider_error
            return _call_groq("system", "synthetic payload", "cv-candidate-fragment-1")

    def test_groq_output_token_overflow_is_permanent_and_sanitized(self):
        raw_message = (
            "Request too large for model synthetic-private-model on output tokens per minute (OTPM): "
            "Limit 1000, Requested 2116. The request's expected output tokens exceed enforced limit. "
            "private organization and CV sentinel"
        )
        error = Exception("provider error")
        error.status_code = 429
        error.body = {"error": {"message": raw_message}}
        error.response = type("Response", (), {"headers": {"Retry-After": "45"}})()

        with self.assertRaises(ProviderOutputLimitExceeded) as caught:
            self._call_with_groq_error(error)

        self.assertEqual(caught.exception.limit_num, 1000)
        self.assertIn("capacidad actual", str(caught.exception))
        self.assertNotIn("synthetic-private-model", str(caught.exception))
        self.assertNotIn("organization", str(caught.exception))
        self.assertNotIn("CV sentinel", str(caught.exception))

    def test_groq_temporary_rate_limit_remains_retryable(self):
        error = Exception("provider error")
        error.status_code = 429
        error.body = {"error": {"message": "Rate limit reached for requests per minute."}}
        error.response = type("Response", (), {"headers": {"Retry-After": "45"}})()

        with self.assertRaises(RateLimitExceeded) as caught:
            self._call_with_groq_error(error)
        self.assertEqual(caught.exception.retry_after_seconds, 45)


if __name__ == "__main__":
    unittest.main()
