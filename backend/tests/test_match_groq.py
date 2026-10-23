import json
import unittest
from unittest.mock import patch


import httpx
from app.core.config import settings
from app.services.match_groq import GroqClient, GroqError


class GroqClientTests(unittest.TestCase):
    def setUp(self):
        limiter = patch('app.services.match_groq.groq_rate_limiter')
        self.limiter = limiter.start().return_value
        self.addCleanup(limiter.stop)

    @patch.object(settings, 'GROQ_API_KEY', 'synthetic-key')
    @patch.object(settings, 'MATCH_PROVIDER', 'groq')
    @patch('app.services.match_groq.httpx.post')
    def test_validates_json_and_uses_groq_endpoint(self, post):
        answers = {key: {'score': 3, 'confidence': 0.7} for key in
                   ['mandatory_skills', 'experience', 'education_and_languages', 'preferences_and_desirables']}
        answers['exclusionary_requirements'] = {'noul': 1}
        post.return_value = httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(answers)}}], 'model': 'test'})
        result = GroqClient().evaluate({'candidate_profile': {}, 'job_offer': {}}, {})
        self.assertEqual(result['answers'], answers)
        self.assertEqual(post.call_args.args[0], 'https://api.groq.com/openai/v1/chat/completions')
        self.assertEqual(post.call_args.kwargs['json']['response_format'], {'type': 'json_object'})

    @patch.object(settings, 'GROQ_API_KEY', 'synthetic-key')
    @patch.object(settings, 'MATCH_PROVIDER', 'groq')
    @patch('app.services.match_groq.httpx.post')
    def test_incomplete_evaluation_is_rejected(self, post):
        post.return_value = httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})
        with self.assertRaises(GroqError):
            GroqClient().evaluate({}, {})

    @patch.object(settings, 'GROQ_API_KEY', 'synthetic-key')
    @patch.object(settings, 'MATCH_PROVIDER', 'groq')
    @patch('app.services.match_groq.httpx.post')
    def test_provider_error_does_not_expose_response_body(self, post):
        post.return_value = httpx.Response(500, text='private content')
        with self.assertRaisesRegex(GroqError, 'HTTP 500') as error:
            GroqClient().evaluate({}, {})
        self.assertNotIn('private content', str(error.exception))

    @patch.object(settings, 'LLM_PROVIDER', 'gemini')
    @patch.object(settings, 'MATCH_PROVIDER', 'groq')
    @patch.object(settings, 'GROQ_API_KEY', 'synthetic-key')
    @patch.object(settings, 'MATCH_GROQ_MODEL', 'qwen/qwen3.8-27b')
    @patch('app.services.match_groq.httpx.post')
    def test_match_uses_groq_independently_and_requests_strict_schema(self, post):
        answer = {'assessments': [{'requirement_id': 'r1', 'status': 'unknown',
                                   'explanation': 'No hay evidencia.', 'evidence_refs': []}]}
        post.return_value = httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(answer)}}],
                                                     'model': 'qwen/qwen3.8-27b'})
        result = GroqClient().evaluate_v2({'job_offer': {'requirements': [{'id': 'r1'}]}})
        self.assertEqual(result['assessments'], answer['assessments'])
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['model'], 'qwen/qwen3.8-27b')
        self.assertEqual(payload['response_format']['json_schema']['strict'], True)
        self.assertEqual(payload['reasoning_effort'], 'low')

    @patch.object(settings, 'MATCH_PROVIDER', 'groq')
    @patch.object(settings, 'GROQ_API_KEY', 'synthetic-key')
    @patch('app.services.match_groq.httpx.post')
    def test_large_offer_is_evaluated_in_bounded_batches(self, post):
        def reply(_url, **kwargs):
            request = json.loads(kwargs['json']['messages'][1]['content'])
            items = [{'requirement_id': item['id'], 'status': 'unknown',
                      'explanation': 'Sin evidencia.', 'evidence_refs': []}
                     for item in request['job_offer']['requirements']]
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'assessments': items})}}],
                                             'model': 'qwen/qwen3.8-27b', 'usage': {'total_tokens': 100}})
        post.side_effect = reply
        requirements = [{'id': str(index)} for index in range(5)]
        result = GroqClient().evaluate_v2({'job_offer': {'requirements': requirements}})
        self.assertEqual([item['requirement_id'] for item in result['assessments']], [str(i) for i in range(5)])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result['usage']['total_tokens'], 200)
