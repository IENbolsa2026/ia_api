import json
import unittest
from unittest.mock import MagicMock, patch


import httpx
from app.core.config import settings
from app.services.match_groq import GroqClient, GroqError
from app.services.rate_limiter import RateLimitExceeded
from app.tasks.match_tasks import evaluate_match_task
from celery.exceptions import Retry


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
        requirements = [{'id': str(index)} for index in range(6)]
        result = GroqClient().evaluate_v2({'job_offer': {'requirements': requirements}})
        self.assertEqual([item['requirement_id'] for item in result['assessments']], [str(i) for i in range(6)])
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result['usage']['total_tokens'], 200)

    @patch.object(settings, 'MATCH_PROVIDER', 'groq')
    @patch.object(settings, 'GROQ_API_KEY', 'synthetic-key')
    @patch('app.services.match_groq.httpx.post')
    def test_quota_retry_resumes_after_saved_batch_without_repeating_it(self, post):
        requirements = [{'id': str(index)} for index in range(6)]
        state = {'job_offer': {'requirements': requirements}}
        saved = []
        sent_ids = []

        def reply(_url, **kwargs):
            request = json.loads(kwargs['json']['messages'][1]['content'])
            ids = [item['id'] for item in request['job_offer']['requirements']]
            sent_ids.append(ids)
            answer = {'assessments': [
                {'requirement_id': item_id, 'status': 'unknown', 'explanation': 'Sin evidencia.', 'evidence_refs': []}
                for item_id in ids
            ]}
            return httpx.Response(200, json={
                'choices': [{'message': {'content': json.dumps(answer)}}],
                'model': 'synthetic-model', 'usage': {'total_tokens': 100},
            })

        post.side_effect = reply
        self.limiter.reserve.side_effect = [None, RateLimitExceeded(12, reason='cooldown'), None]
        with self.assertRaises(RateLimitExceeded):
            GroqClient().evaluate_v2(state, on_batch_completed=lambda batches: saved.extend(batches))
        self.assertEqual(len(saved), 1)
        result = GroqClient().evaluate_v2(state, completed_batches=saved)
        self.assertEqual(sent_ids, [['0', '1', '2', '3', '4'], ['5']])
        self.assertEqual([item['requirement_id'] for item in result['assessments']], [str(i) for i in range(6)])
        self.assertEqual(result['usage']['total_tokens'], 200)

    def test_task_persists_completed_batch_across_quota_retry(self):
        state = {
            'status': 'accepted',
            'request': {
                'protocol_version': 'match.v2',
                'candidate_profile': {},
                'job_offer': {'requirements': [{'id': str(i)} for i in range(6)]},
                'metadata': {'evaluation_date': '2026-10-24'},
            },
        }
        saved_batch = {'assessments': [
            {'requirement_id': str(i), 'status': 'unknown', 'explanation': 'Sin evidencia.', 'evidence_refs': []}
            for i in range(5)
        ], 'model': 'synthetic-model', 'usage': {'total_tokens': 100}}
        store = MagicMock()
        store.get.side_effect = lambda _request_id: state.copy()
        store.acquire_lock.return_value = True
        store.update_owned.side_effect = lambda _request_id, **changes: state.update(changes)

        def first_evaluation(_payload, *, completed_batches, on_batch_completed):
            self.assertIsNone(completed_batches)
            on_batch_completed([saved_batch])
            raise RateLimitExceeded(12, reason='cooldown')

        with patch('app.tasks.match_tasks.MatchJobStore', return_value=store), \
             patch('app.tasks.match_tasks.GroqClient') as provider, \
             patch.object(evaluate_match_task, 'retry', side_effect=Retry('deferred')):
            provider.return_value.evaluate_v2.side_effect = first_evaluation
            evaluate_match_task.push_request(retries=0)
            try:
                with self.assertRaises(Retry):
                    evaluate_match_task.run('synthetic-request')
            finally:
                evaluate_match_task.pop_request()

        self.assertEqual(state['batch_checkpoint'], [saved_batch])
        self.assertEqual(state['status'], 'rate_limited')

        with patch('app.tasks.match_tasks.MatchJobStore', return_value=store), \
             patch('app.tasks.match_tasks.GroqClient') as provider, \
             patch('app.tasks.match_tasks.build_result') as scoring:
            provider.return_value.evaluate_v2.return_value = {
                'assessments': saved_batch['assessments'], 'model': 'synthetic-model', 'usage': {'total_tokens': 100},
            }
            scoring.return_value.model_dump.return_value = {'match_percentage': 50}
            result = evaluate_match_task.run('synthetic-request')
            self.assertEqual(provider.return_value.evaluate_v2.call_args.kwargs['completed_batches'], [saved_batch])
        self.assertEqual(result, {'match_percentage': 50})
        self.assertEqual(state['status'], 'completed')


class MatchInputBudgetTests(unittest.TestCase):
    def test_pointer_patterns_preserve_facts_and_cover_every_reference(self):
        from app.services.match_scoring import candidate_evidence_index
        profile = {'skills': [{'name': f'Skill {i}', 'level': 'unknown', 'evidence': ['Literal factual evidence.'],
                              'source_quotes': ['Literal factual evidence.']} for i in range(30)]}
        index = candidate_evidence_index(profile)
        captured = []
        def post(payload):
            captured.append(json.loads(payload['messages'][1]['content']))
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"assessments":[]}'}}]})
        with patch.object(GroqClient, '_require_key'), patch.object(GroqClient, '_post', side_effect=post):
            GroqClient()._evaluate_v2_batch({'candidate_profile': profile, 'candidate_evidence_index': index,
                                           'job_offer': {'requirements': []}})
        compact = captured[0]['candidate_evidence_index']
        patterns = compact['paths']
        self.assertEqual(set(patterns), {'/'.join('*' if part.isdecimal() else part for part in pointer.split('/')) for pointer in index})
        self.assertEqual(compact['format'], 'profile-json-pointer-patterns.v1')
        self.assertEqual(captured[0]['candidate_profile'], profile)
        self.assertLess(len(json.dumps(compact)), len(json.dumps(sorted(index))))

    def test_oversized_requirement_batch_splits_without_truncating_profile(self):
        from app.services.rate_limiter import ProviderInputLimitExceeded
        profile = {'skills': [{'name': 'Python', 'source_quotes': ['Usa Python.']}]}
        sent = []
        def post(payload):
            state = json.loads(payload['messages'][1]['content'])
            self.assertEqual(state['candidate_profile'], profile)
            ids = [item['id'] for item in state['job_offer']['requirements']]
            sent.append(ids)
            if len(ids) > 2:
                raise ProviderInputLimitExceeded(8313, 7000)
            result = {'assessments': [{'requirement_id': key, 'status': 'unknown',
                                     'explanation': 'Sin evidencia.', 'evidence_refs': []} for key in ids]}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}],
                                           'usage': {'total_tokens': 10}})
        state = {'candidate_profile': profile, 'candidate_evidence_index': {},
                 'job_offer': {'requirements': [{'id':str(i)} for i in range(5)]}}
        with patch.object(GroqClient, '_require_key'), patch.object(GroqClient, '_post', side_effect=post):
            response = GroqClient().evaluate_v2(state)
        self.assertEqual([item['requirement_id'] for item in response['assessments']], [str(i) for i in range(5)])
        self.assertEqual(response['usage']['total_tokens'], 30)
        self.assertEqual(sent, [['0','1','2','3','4'], ['0','1'], ['2','3','4'], ['2'], ['3','4']])

    def test_split_progress_survives_quota_wait_without_repeating_completed_requirements(self):
        from app.services.rate_limiter import ProviderInputLimitExceeded
        checkpoint = []
        sent = []
        waiting = True
        def post(payload):
            nonlocal waiting
            ids = [row['id'] for row in json.loads(payload['messages'][1]['content'])['job_offer']['requirements']]
            sent.append(ids)
            if len(ids) > 2:
                raise ProviderInputLimitExceeded(8313, 7000)
            if ids == ['2'] and waiting:
                waiting = False
                raise RateLimitExceeded(12, reason='cooldown')
            result = {'assessments': [{'requirement_id': key, 'status': 'unknown',
                                     'explanation': 'Sin evidencia.', 'evidence_refs': []} for key in ids]}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})
        state = {'candidate_profile': {}, 'candidate_evidence_index': {},
                 'job_offer': {'requirements': [{'id':str(i)} for i in range(5)]}}
        def save(parts):
            checkpoint[:] = parts
        with patch.object(GroqClient, '_require_key'), patch.object(GroqClient, '_post', side_effect=post):
            with self.assertRaises(RateLimitExceeded):
                GroqClient().evaluate_v2(state, on_batch_completed=save)
            self.assertEqual([row['requirement_id'] for row in checkpoint[0]['assessments']], ['0','1'])
            result = GroqClient().evaluate_v2(state, completed_batches=checkpoint, on_batch_completed=save)
        self.assertEqual(sent.count(['0','1']), 1)
        self.assertEqual(len(result['assessments']), 5)

    def test_single_requirement_over_capacity_fails_once_with_public_message(self):
        from app.services.rate_limiter import ProviderInputLimitExceeded
        job = {'status': 'accepted', 'request': {'protocol_version': 'match.v2',
               'candidate_profile': {}, 'job_offer': {'requirements': [{'id': 'one'}]},
               'metadata': {'evaluation_date': '2026-11-25'}}}
        store = MagicMock()
        store.get.side_effect = lambda _key: job.copy()
        store.update_owned.side_effect = lambda _key, **changes: job.update(changes)
        with patch('app.tasks.match_tasks.MatchJobStore', return_value=store), \
             patch('app.tasks.match_tasks.GroqClient') as client, \
             patch.object(evaluate_match_task, 'retry') as retry:
            client.return_value.evaluate_v2.side_effect = ProviderInputLimitExceeded(9000, 7000)
            result = evaluate_match_task.run('synthetic-request')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('capacidad disponible', job['error'])
        retry.assert_not_called()
