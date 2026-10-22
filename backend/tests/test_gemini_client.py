"""Gemini adapter contract, safe errors, scheduling and existing flow routing."""

import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.core.config import Settings, settings
from app.services.cv_pipeline import _call_provider, process_cv_text
from app.services.cv_compact_contract import fragment_schema
from app.services.cv_consolidation import ConsolidationPlan
from app.services.gemini_cv_schema import flatten_cv_schema
from app.services.gemini_client import GeminiError, _retry_delay, generate_json, gemini_rate_limiter
from app.services.match_groq import GroqClient, GroqError
from app.services.rate_limiter import ProviderResponseTruncated, RateLimitExceeded, groq_rate_limiter


def reply(content=None, finish='STOP', **extra):
    return {
        'candidates': [{'finishReason': finish, 'content': {'parts': [
            {'thought': True, 'text': 'private internal thought sentinel'},
            {'text': json.dumps(content if content is not None else {'ok': True})},
        ]}}], 'modelVersion': 'gemini-3.5-flash-lite', **extra,
    }


class GeminiClientTests(unittest.TestCase):
    def setUp(self):
        for name, value in {
            'LLM_PROVIDER': 'gemini', 'GEMINI_API_KEY': 'synthetic-gemini-key',
            'GEMINI_MODEL': 'gemini-3.5-flash-lite', 'GEMINI_MAX_OUTPUT_TOKENS': 8192,
        }.items():
            setting = patch.object(settings, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        limiter = patch('app.services.gemini_client.gemini_rate_limiter')
        self.limiter = limiter.start().return_value
        self.limiter.record_response.return_value = 15
        self.addCleanup(limiter.stop)
        post = patch('app.services.gemini_client.httpx.post')
        self.post = post.start()
        self.addCleanup(post.stop)
        self.post.return_value = httpx.Response(200, json=reply())

    def test_generate_content_uses_header_key_json_mime_minimal_thinking_and_safe_usage(self):
        self.post.return_value = httpx.Response(200, json=reply(usageMetadata={
            'promptTokenCount': 12, 'candidatesTokenCount': 7, 'totalTokenCount': 19,
            'thoughtsTokenCount': True, 'unexpected': 'private CV sentinel',
        }))
        with self.assertLogs('app.services.gemini_client', level='INFO') as logs:
            result = generate_json('private system sentinel', 'private CV sentinel', 'synthetic')
        self.assertEqual(result['content'], {'ok': True})
        self.assertEqual(result['usage'], {'prompt_tokens': 12, 'completion_tokens': 7, 'total_tokens': 19})
        self.assertEqual(result['model'], 'gemini-3.5-flash-lite')
        url = self.post.call_args.args[0]
        self.assertEqual(url, 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent')
        self.assertNotIn('synthetic-gemini-key', url)
        request = self.post.call_args.kwargs
        self.assertEqual(request['headers']['x-goog-api-key'], 'synthetic-gemini-key')
        config = request['json']['generationConfig']
        self.assertEqual(config['maxOutputTokens'], 8192)
        self.assertEqual(config['responseMimeType'], 'application/json')
        self.assertEqual(config['thinkingConfig'], {'thinkingLevel': 'minimal'})
        self.assertNotIn('responseJsonSchema', config)
        self.assertNotIn('responseSchema', config)
        self.assertEqual(request['json']['systemInstruction']['parts'][0]['text'], 'private system sentinel')
        self.assertNotIn('sentinel', str(logs.output))
        self.assertNotIn('synthetic-gemini-key', str(logs.output))
        self.limiter.reserve.assert_called_once()
        self.limiter.record_response.assert_called_once()

    def test_gemini_25_disables_thinking_and_respects_explicit_output_budget(self):
        with patch.object(settings, 'GEMINI_MODEL', 'gemini-2.5-flash-lite'):
            generate_json('system', 'user', 'synthetic', 950)
        config = self.post.call_args.kwargs['json']['generationConfig']
        self.assertEqual(config['thinkingConfig'], {'thinkingBudget': 0})
        self.assertEqual(config['maxOutputTokens'], 950)

    def test_transport_constrains_real_cv_schemas_and_leaves_matching_plain_json(self):
        packet_schema = fragment_schema()
        plan_schema = ConsolidationPlan.model_json_schema(by_alias=True)
        cases = [
            ({'packet_id': 'synthetic-packet', 'text': 'CV ficticio.',
              'source_quotes': {'quote_0': 'CV ficticio.'}, 'fragment_schema': packet_schema}, packet_schema),
            ({'candidate_id': '42', 'candidate_profile_version': 'candidate-profile.v2',
              'facts': {'sk': [], 'x': [], 'ed': [], 'l': [], 'c': []}, 'plan_schema': plan_schema}, plan_schema),
            ({'job_offer': {'requirements': []}, 'candidate_profile': {'skills': []}}, None),
        ]
        for payload, expected in cases:
            with self.subTest(envelope=list(payload)):
                serialized = json.dumps(payload, ensure_ascii=False)
                generate_json('Synthetic system.', serialized, 'synthetic-transport')
                sent = self.post.call_args.kwargs['json']
                config = sent['generationConfig']
                self.assertEqual(config['responseMimeType'], 'application/json')
                self.assertEqual(config['thinkingConfig'], {'thinkingLevel': 'minimal'})
                self.assertEqual(sent['contents'][0]['parts'][0]['text'], serialized)
                if expected is None:
                    self.assertNotIn('responseJsonSchema', config)
                else:
                    expected_shape = flatten_cv_schema(expected)
                    if 'fragment_schema' in payload:
                        expected_shape['required'] = ['sk']
                        expected_shape['properties']['sk']['items']['required'] = ['n', 'k', 'l', 'o', 'e', 'q']
                    self.assertEqual(config['responseJsonSchema'], expected_shape)
                self.assertNotIn('responseSchema', config)
        self.assertEqual(self.post.call_count, 3)  # One request per admission; no hidden retry.

    def test_max_tokens_is_not_accepted_as_a_complete_json_profile(self):
        self.post.return_value = httpx.Response(200, json=reply({'apparently': 'valid'}, finish='MAX_TOKENS'))
        with self.assertRaises(ProviderResponseTruncated):
            generate_json('system', 'user', 'synthetic')

    def test_invalid_blocked_or_missing_json_is_sanitized(self):
        bodies = [
            [], {'promptFeedback': {'blockReason': 'private CV sentinel'}},
            reply(finish='SAFETY'), reply(['unexpected array']),
            {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'private CV sentinel'}]}}]},
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.post.return_value = httpx.Response(200, json=body)
                with self.assertRaises(GeminiError) as error:
                    generate_json('system', 'user', 'synthetic')
                self.assertNotIn('sentinel', str(error.exception))

    def test_429_retry_info_and_header_share_cooldown_without_hidden_retries(self):
        self.post.return_value = httpx.Response(429, headers={'Retry-After': '2'}, json={
            'error': {'message': 'private quota and organization sentinel', 'details': [{
                '@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '37.25s',
            }]},
        })
        self.limiter.record_response.return_value = 37.25
        with self.assertRaises(RateLimitExceeded) as error:
            generate_json('system', 'user', 'synthetic')
        self.assertEqual(error.exception.retry_after_seconds, 37.25)
        self.assertFalse(error.exception.is_local_wait)
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.limiter.record_response.call_args.args[0], {'Retry-After': '37.25'})
        self.assertNotIn('sentinel', str(error.exception))

    def test_retry_info_cannot_shorten_retry_after_and_malformed_details_are_ignored(self):
        response = httpx.Response(429, headers={'Retry-After': '45'}, json={'error': {'details': [{
            '@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': {'seconds': '7', 'nanos': 500000000},
        }]}})
        self.assertEqual(_retry_delay(response), 45)
        self.assertEqual(_retry_delay(httpx.Response(429, json={'error': {'details': ['private sentinel']}})), 60)

    def test_daily_provider_quota_defers_until_reset_without_repeated_short_retries(self):
        self.post.return_value = httpx.Response(429, json={'error': {'details': [{
            '@type': 'type.googleapis.com/google.rpc.QuotaFailure',
            'violations': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}],
        }]}})
        with patch('app.services.gemini_client._until_daily_reset', return_value=50000):
            with self.assertRaises(RateLimitExceeded) as raised:
                generate_json('system', 'user', 'synthetic')
        self.assertEqual(raised.exception.reason, 'daily_budget')
        self.assertTrue(raised.exception.is_local_wait)
        self.assertEqual(raised.exception.retry_after_seconds, 50000)
        self.post.assert_called_once()

    def test_long_retry_info_remains_global_even_above_generic_header_bound(self):
        self.post.return_value = httpx.Response(429, json={'error': {'details': [{
            '@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '5000s',
        }]}})
        with self.assertRaises(RateLimitExceeded) as error:
            generate_json('system', 'user', 'synthetic')
        self.assertEqual(error.exception.retry_after_seconds, 5000)
        self.assertEqual(self.limiter.client.eval.call_args.args[-3], 5_000_000)

    def test_local_wait_precedes_http_and_http_errors_never_echo_provider_body(self):
        self.limiter.reserve.side_effect = RateLimitExceeded(15, reason='cooldown')
        with self.assertRaises(RateLimitExceeded):
            generate_json('system', 'user', 'synthetic')
        self.post.assert_not_called()
        self.limiter.reserve.side_effect = None
        self.post.return_value = httpx.Response(403, text='private credential and CV sentinel')
        with self.assertRaisesRegex(GeminiError, 'HTTP 403') as error:
            generate_json('system', 'user', 'synthetic')
        self.assertNotIn('sentinel', str(error.exception))

    def test_transport_errors_and_missing_credentials_are_safe(self):
        self.post.side_effect = httpx.ConnectError('private credential sentinel')
        with self.assertRaises(GeminiError) as error:
            generate_json('system', 'user', 'synthetic')
        self.assertNotIn('sentinel', str(error.exception))
        self.post.reset_mock()
        with patch.object(settings, 'GEMINI_API_KEY', ''):
            with self.assertRaises(GeminiError):
                generate_json('system', 'user', 'synthetic')
        self.post.assert_not_called()

    def test_gemini_cv_dispatch_keeps_existing_profile_shape_and_provider_model(self):
        self.post.return_value = httpx.Response(200, json=reply({'sk': []}))
        result = _call_provider('system', 'user', 'synthetic', 8192)
        self.assertEqual(result, {'sk': [], '__provider_model': 'gemini-3.5-flash-lite'})

    def test_existing_matcher_routes_v1_and_v2_through_gemini_and_keeps_local_validation(self):
        dimensions = {key: {'score': 3, 'confidence': .5} for key in (
            'mandatory_skills', 'experience', 'education_and_languages', 'preferences_and_desirables',
        )}
        dimensions['exclusionary_requirements'] = {'noul': 1}
        self.post.return_value = httpx.Response(200, json=reply(dimensions))
        legacy = GroqClient().evaluate({}, {})
        self.assertEqual(legacy['answers'], dimensions)
        self.assertEqual(legacy['model'], settings.GEMINI_MODEL)
        expected = {'assessments': [{'requirement_id': 'r1', 'status': 'unknown',
                                     'explanation': 'Falta evidencia.', 'evidence_refs': []}]}
        self.post.return_value = httpx.Response(200, json=reply(expected))
        result = GroqClient().evaluate_v2({'job_offer': {'requirements': [{'id': 'r1'}]}})
        self.assertEqual(result['assessments'], expected['assessments'])
        self.post.return_value = httpx.Response(200, json=reply({'assessments': [{'requirement_id': 'r1'}]}))
        with self.assertRaises(GroqError):
            GroqClient().evaluate_v2({'job_offer': {'requirements': [{'id': 'r1'}]}})


class GeminiScopeTests(unittest.TestCase):
    def test_gemini_settings_load_from_environment_without_local_env_file(self):
        with patch.dict(os.environ, {
            'LLM_PROVIDER': 'gemini', 'GEMINI_API_KEY': 'synthetic-environment-key',
            'GEMINI_MODEL': 'gemini-2.5-flash-lite', 'GEMINI_REQUESTS_PER_MINUTE': '3',
            'GEMINI_PAUSA_SEGUNDOS': '20', 'GEMINI_MAX_OUTPUT_TOKENS': '4096',
        }, clear=True):
            configured = Settings(_env_file=None)
        self.assertEqual(configured.LLM_PROVIDER, 'gemini')
        self.assertEqual(configured.GEMINI_API_KEY, 'synthetic-environment-key')
        self.assertEqual(configured.GEMINI_MODEL, 'gemini-2.5-flash-lite')
        self.assertEqual(configured.GEMINI_REQUESTS_PER_MINUTE, 3)
        self.assertEqual(configured.GEMINI_PAUSA_SEGUNDOS, 20)
        self.assertEqual(configured.GEMINI_MAX_OUTPUT_TOKENS, 4096)

    def test_gemini_quota_is_separate_from_groq_and_other_models_and_credentials(self):
        with patch.object(settings, 'GEMINI_API_KEY', 'synthetic-one'):
            first = gemini_rate_limiter('gemini-3.5-flash-lite')
            other_model = gemini_rate_limiter('gemini-2.5-flash-lite')
        with patch.object(settings, 'GEMINI_API_KEY', 'synthetic-two'):
            other_key = gemini_rate_limiter('gemini-3.5-flash-lite')
        groq = groq_rate_limiter('synthetic-groq')
        self.assertNotEqual(first.rate_key, groq.rate_key)
        self.assertNotEqual(first.cooldown_key, other_model.cooldown_key)
        self.assertNotEqual(first.cooldown_key, other_key.cooldown_key)
        self.assertNotIn('synthetic-one', first.cooldown_key)


class GeminiPacketIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_packet_identity_uses_gemini_configured_model_with_full_output_budget(self):
        checkpoints = []

        async def provider(_system, _payload, marker, max_output_tokens=None):
            self.assertEqual(max_output_tokens, 8192)
            return {'h': None, 's': None, 'sk': [], 'x': [], 'ed': [], 'l': [], 'c': []} if marker.endswith('consolidation-plan') else {}

        with patch.object(settings, 'LLM_PROVIDER', 'gemini'), \
             patch.object(settings, 'GEMINI_MODEL', 'gemini-3.5-flash-lite'), \
             patch.object(settings, 'CV_EXTRACTION_MAX_TOKENS', 8192), \
             patch('app.services.cv_pipeline._call_async', new_callable=AsyncMock, side_effect=provider):
            result = await process_cv_text('Synthetic source.', 42, on_checkpoint=checkpoints.append)
        self.assertEqual(result['profile_id'], '42')
        self.assertEqual(checkpoints[0]['identity']['provider'], 'gemini')
        self.assertEqual(checkpoints[0]['identity']['model'], 'gemini-3.5-flash-lite')
        self.assertEqual(checkpoints[0]['identity']['budget'], 8192)
