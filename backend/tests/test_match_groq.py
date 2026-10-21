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
    @patch('app.services.match_groq.httpx.post')
    def test_incomplete_evaluation_is_rejected(self, post):
        post.return_value = httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})
        with self.assertRaises(GroqError):
            GroqClient().evaluate({}, {})

    @patch.object(settings, 'GROQ_API_KEY', 'synthetic-key')
    @patch('app.services.match_groq.httpx.post')
    def test_provider_error_does_not_expose_response_body(self, post):
        post.return_value = httpx.Response(500, text='private content')
        with self.assertRaisesRegex(GroqError, 'HTTP 500') as error:
            GroqClient().evaluate({}, {})
        self.assertNotIn('private content', str(error.exception))
