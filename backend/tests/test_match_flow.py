"""Flujo WebSocket → worker → puntuación con un proveedor simulado."""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4


from fastapi.testclient import TestClient
from app.main import app
from app.core.config import settings
from app.tasks.match_tasks import evaluate_match_task


class MemoryStore:
    def __init__(self):
        self.jobs = {}

    def create_or_get(self, request):
        key = request['request_id']
        created = key not in self.jobs
        if created:
            self.jobs[key] = {'status': 'accepted', 'request': deepcopy(request), 'result': None, 'error': None}
        return self.jobs[key], created

    def get(self, key):
        return deepcopy(self.jobs.get(key))

    def update(self, key, **changes):
        self.jobs[key].update(changes)

    def acquire_lock(self, key):
        return True

    def release_lock(self, key):
        pass


class MatchFlowTests(unittest.TestCase):
    def test_authenticated_socket_returns_percentage_and_recovers_same_result(self):
        request_id = str(uuid4())
        request = {
            'request_id': request_id,
            'candidate_profile': {'profile_id': '1', 'skills': [{'name': 'Python', 'level': 'advanced'}]},
            'job_offer': {'offer_id': '2', 'title': 'Python developer', 'requirements': [{'name': 'Python', 'minimum_level': 'intermediate'}]},
            'metadata': {'candidate_profile_hash': 'a'*64, 'candidate_profile_version': 1,
                         'offer_profile_hash': 'b'*64, 'offer_profile_version': 1,
                         'rubric_version': 'match-rubric.v1', 'matcher_prompt_version': 'match-rubric.v1', 'model': 'groq:test-model'},
        }
        answers = {key: {'score': 3, 'confidence': 0.8} for key in
                   ['mandatory_skills', 'experience', 'education_and_languages', 'preferences_and_desirables']}
        answers['exclusionary_requirements'] = {'noul': 1}
        store = MemoryStore()
        with patch.object(settings, 'SERVICE_API_TOKEN', 'test-service'), \
             patch('app.api.v1.endpoints.matches.MatchJobStore', return_value=store), \
             patch('app.tasks.match_tasks.MatchJobStore', return_value=store), \
             patch('app.tasks.match_tasks.RedisRateLimiter') as limiter, \
             patch('app.tasks.match_tasks.GroqClient') as provider, \
             patch('app.api.v1.endpoints.matches.celery_app.send_task') as send:
            limiter.return_value.reserve.return_value = SimpleNamespace(granted=True)
            provider.return_value.evaluate.return_value = {'answers': answers}
            send.side_effect = lambda name, args, **kwargs: evaluate_match_task.run(*args)
            with TestClient(app) as client:
                schema = client.get('/api/v1/openapi.json').json()
                self.assertIn('/api/v1/cv-analyses', schema['paths'])
                self.assertIn('post', schema['paths']['/api/v1/matches'])
                with client.websocket_connect('/api/v1/ws/v1/matches', headers={'Authorization': 'Bearer test-service'}) as socket:
                    socket.send_json({'type': 'match.start', 'payload': request})
                    self.assertEqual(socket.receive_json()['status'], 'completed')
                    completed = socket.receive_json()
                    self.assertEqual(completed['result']['match_percentage'], 75)
                    self.assertEqual(completed['result']['assessments'][0]['status'], 'met')
                recovered = client.get(f'/api/v1/matches/{request_id}', headers={'Authorization': 'Bearer test-service'})
                self.assertEqual(recovered.json()['result']['match_percentage'], 75)
                duplicate = client.post('/api/v1/matches', json=request, headers={'Authorization': 'Bearer test-service'})
                self.assertEqual(duplicate.status_code, 202)
                self.assertFalse(duplicate.json()['created'])
                self.assertEqual(client.get(f'/api/v1/matches/{request_id}').status_code, 401)
                provider.return_value.evaluate.assert_called_once()
