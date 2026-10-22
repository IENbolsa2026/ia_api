"""Temporary Gemini outages preserve CV checkpoints and use bounded queued retries."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

from celery.exceptions import Retry

from app.services.gemini_client import GeminiUnavailable
from app.services.rate_limiter import RedisRateLimiter
from app.tasks.cv_analysis_tasks import run_extraction_task
from app.tasks.match_tasks import evaluate_match_task


class ProviderOutageTests(unittest.TestCase):
    def test_shared_cooldown_preserves_quota_and_longer_existing_wait(self):
        client = MagicMock()
        client.eval.return_value = 90_000
        limiter = RedisRateLimiter(client, 12, provider='gemini', cooldown_scope='synthetic')
        self.assertEqual(limiter.defer(30), 90)
        args = client.eval.call_args.args
        self.assertEqual(args[1:4], (2, limiter.cooldown_key, limiter.quota_key))
        self.assertEqual(args[-3:], (30_000, '', ''))

    def test_retry_backoff_is_bounded_and_never_shortens_provider_hint(self):
        with patch('app.services.gemini_client.random.uniform', return_value=5):
            self.assertEqual([GeminiUnavailable().retry_countdown(n) for n in range(1, 6)], [35, 65, 125, 245, 305])
            self.assertEqual(GeminiUnavailable(600).retry_countdown(1), 605)

    def test_cv_progress_resets_outage_budget_and_next_attempt_resumes_checkpoint(self):
        initial = {'packets': {'p1': {'status': 'completed'}}}
        advanced = {'packets': {'p1': {'status': 'completed'}, 'p2': {'status': 'completed'}}}
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / 'cv.txt'
            artifact.write_bytes(b'Synthetic sales CV.')
            state = {'job_id': 'synthetic-cv', 'status': 'accepted',
                'provider_unavailable_failures': 4, 'extraction_packets': initial,
                'payload': {'kind': 'candidate_cv', 'analysis_id': 10, 'candidate_id': 20,
                    'artifact_id': 'synthetic-artifact', 'format': 'txt',
                    'source_hash': hashlib.sha256(artifact.read_bytes()).hexdigest()}}
            store = MagicMock()
            store.get.side_effect = lambda _: state.copy()
            store.acquire_lock.return_value = True
            store.update_owned.side_effect = lambda _, **changes: state.update(changes)

            async def process(*args, **kwargs):
                if kwargs['checkpoint'] == initial:
                    kwargs['on_checkpoint'](advanced)
                    raise GeminiUnavailable(90)
                self.assertEqual(kwargs['checkpoint'], advanced)
                return {'skills': []}

            with patch('app.tasks.cv_analysis_tasks.ExtractionJobStore', return_value=store), \
                 patch('app.tasks.cv_analysis_tasks.artifact_path', return_value=artifact), \
                 patch('app.tasks.cv_analysis_tasks.extract_in_subprocess', return_value='Synthetic sales CV.'), \
                 patch('app.tasks.cv_analysis_tasks.process_cv_text', side_effect=process), \
                 patch('app.tasks.cv_analysis_tasks.delete_artifact') as delete, \
                 patch('app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay') as delivery, \
                 patch('app.services.gemini_client.random.uniform', return_value=0):
                run_extraction_task.push_request(id='synthetic-task', retries=50, called_directly=False,
                    is_eager=True, args=('synthetic-cv',), kwargs={})
                try:
                    with self.assertRaises(Retry) as raised:
                        run_extraction_task.run('synthetic-cv')
                finally:
                    run_extraction_task.pop_request()
                self.assertEqual(raised.exception.when, 90)
                self.assertEqual(state['status'], 'processing')
                self.assertEqual(state['provider_unavailable_failures'], 1)
                self.assertEqual(state['extraction_packets'], advanced)
                self.assertIsNone(state['error'])
                delete.assert_not_called()
                delivery.assert_not_called()
                store.release_lock.assert_has_calls([call('synthetic-cv'), call('__cv_serial__')])
                result = run_extraction_task.run('synthetic-cv')
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(state['result'], {'skills': []})
                delivery.assert_called_once_with('synthetic-cv')
                delete.assert_called_once_with('synthetic-artifact', 'txt')

    def test_extraction_outage_exhaustion_reports_provider_and_sends_one_failure(self):
        state = {'job_id': 'synthetic-offer', 'status': 'processing', 'provider_unavailable_failures': 4,
            'payload': {'kind': 'job_offer', 'offer_id': 42, 'offer_snapshot': {}}}
        store = MagicMock()
        store.get.side_effect = lambda _: state.copy()
        store.acquire_lock.return_value = True
        store.update_owned.side_effect = lambda _, **changes: state.update(changes)
        with patch('app.tasks.cv_analysis_tasks.ExtractionJobStore', return_value=store), \
             patch('app.tasks.cv_analysis_tasks.process_offer_snapshot', new_callable=AsyncMock, side_effect=GeminiUnavailable()), \
             patch('app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay') as delivery:
            result = run_extraction_task.run('synthetic-offer')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(state['provider_unavailable_failures'], 5)
        self.assertIn('Gemini', state['error'])
        self.assertNotIn('Revisá el CV', state['error'])
        delivery.assert_called_once_with('synthetic-offer')
        store.release_lock.assert_called_once_with('synthetic-offer')

    def test_match_outage_retries_with_backoff_and_is_bounded_independently_of_quota_waits(self):
        state = {'request_id': 'synthetic-match', 'status': 'processing', 'request': {
            'candidate_profile': {}, 'job_offer': {}}, 'provider_unavailable_failures': 1}
        store = MagicMock()
        store.get.side_effect = lambda _: state.copy()
        store.acquire_lock.return_value = True
        store.update_owned.side_effect = lambda _, **changes: state.update(changes)
        with patch('app.tasks.match_tasks.MatchJobStore', return_value=store), \
             patch('app.tasks.match_tasks.GroqClient') as provider, \
             patch.object(evaluate_match_task, 'retry', side_effect=Retry('deferred')) as retry, \
             patch('app.services.gemini_client.random.uniform', return_value=0):
            provider.return_value.evaluate.side_effect = GeminiUnavailable()
            with self.assertRaises(Retry):
                evaluate_match_task.run('synthetic-match')
            self.assertEqual(retry.call_args.kwargs['countdown'], 60)
            self.assertEqual(state['provider_unavailable_failures'], 2)
            self.assertEqual(state['status'], 'processing')
            state['provider_unavailable_failures'] = 4
            retry.reset_mock()
            result = evaluate_match_task.run('synthetic-match')
            self.assertEqual(result['status'], 'failed')
            self.assertIn('Gemini', state['error'])
            retry.assert_not_called()
