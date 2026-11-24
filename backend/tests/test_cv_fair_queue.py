import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from celery.exceptions import Retry

from app.core.config import settings
from app.services.cv_queue import public_progress, remaining_seconds, retry_delay
from app.services.rate_limiter import RateLimitExceeded
from app.tasks.cv_analysis_tasks import CV_SERIAL_LOCK_ID, run_extraction_task
from tests.test_cv_serial_queue import SharedLockStore, cv_job


class FairQueueTests(unittest.TestCase):
    def test_three_users_take_turns_and_resume_saved_packets(self):
        jobs = {name: cv_job(name) for name in ['first', 'second', 'third']}
        store = SharedLockStore(jobs)
        calls = []
        snapshots = []

        async def process(_text, _candidate, *, identity, checkpoint, on_checkpoint, **kwargs):
            name = identity['job_id']
            calls.append(name)
            if name == 'first' and checkpoint is None:
                partial = {'packets': {'a': {'status': 'completed', 'result': {'fact': 'saved'}},
                                       'b': {'status': 'pending'}}}
                snapshots.append(copy.deepcopy(partial))
                on_checkpoint(partial)
                self.fail('A completed turn must yield before requesting a second packet.')
            if name == 'first':
                self.assertEqual(checkpoint, snapshots[0])
                self.assertEqual(checkpoint['packets']['a']['result'], {'fact': 'saved'})
            return {'schema_version': 'candidate-profile.v2'}

        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / 'cv.pdf'
            artifact.write_bytes(b'synthetic CV artifact')
            with patch('app.tasks.cv_analysis_tasks.ExtractionJobStore', return_value=store), \
                 patch('app.tasks.cv_analysis_tasks.artifact_path', return_value=artifact), \
                 patch('app.tasks.cv_analysis_tasks.extract_in_subprocess', return_value='synthetic text'), \
                 patch('app.tasks.cv_analysis_tasks.process_cv_text', side_effect=process), \
                 patch('app.tasks.cv_analysis_tasks.delete_artifact'), \
                 patch('app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay'), \
                 patch.object(run_extraction_task, 'retry', side_effect=Retry('queued')), \
                 patch.object(settings, 'CV_PACKETS_PER_TURN', 1):
                with self.assertRaises(Retry):
                    run_extraction_task.run('first')
                self.assertEqual(jobs['first']['status'], 'queued')
                self.assertEqual(jobs['first']['wait_reason'], 'queue_turn')
                self.assertNotIn(CV_SERIAL_LOCK_ID, store.locks)
                for name in ['second', 'third', 'first']:
                    self.assertEqual(run_extraction_task.run(name)['status'], 'completed')
        self.assertEqual(calls, ['first', 'second', 'third', 'first'])
        self.assertTrue(all(j['status'] == 'completed' for j in jobs.values()))

    def test_quota_wait_releases_worker_for_another_cv(self):
        jobs = {name: cv_job(name) for name in ['limited', 'next']}
        store = SharedLockStore(jobs)
        async def process(_text, _candidate, *, identity, **kwargs):
            if identity['job_id'] == 'limited':
                raise RateLimitExceeded(86400, reason='daily_budget')
            return {'schema_version': 'candidate-profile.v2'}
        with tempfile.TemporaryDirectory() as folder:
            artifact = Path(folder) / 'cv.pdf'
            artifact.write_bytes(b'synthetic CV artifact')
            with patch('app.tasks.cv_analysis_tasks.ExtractionJobStore', return_value=store), \
                 patch('app.tasks.cv_analysis_tasks.artifact_path', return_value=artifact), \
                 patch('app.tasks.cv_analysis_tasks.extract_in_subprocess', return_value='synthetic text'), \
                 patch('app.tasks.cv_analysis_tasks.process_cv_text', side_effect=process), \
                 patch('app.tasks.cv_analysis_tasks.delete_artifact'), \
                 patch('app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay'), \
                 patch.object(run_extraction_task, 'retry', side_effect=Retry('queued')) as retry:
                with self.assertRaises(Retry):
                    run_extraction_task.run('limited')
                self.assertEqual(jobs['limited']['status'], 'rate_limited')
                self.assertLessEqual(retry.call_args.kwargs['countdown'], 900)
                self.assertFalse(store.locks)
                self.assertEqual(run_extraction_task.run('next')['status'], 'completed')

    def test_expired_wait_returns_failure_without_another_provider_request(self):
        now = datetime(2026, 11, 24, 17, tzinfo=timezone.utc)
        job = cv_job('expired')
        job.update(first_started_at=(now-timedelta(minutes=16)).isoformat(),
                   last_progress_at=(now-timedelta(minutes=16)).isoformat(), status='rate_limited')
        store = SharedLockStore({'expired': job})
        with patch('app.services.cv_queue.utc_now', return_value=now), \
             patch('app.tasks.cv_analysis_tasks.ExtractionJobStore', return_value=store), \
             patch('app.tasks.cv_analysis_tasks.process_cv_text') as provider, \
             patch('app.tasks.cv_analysis_tasks.delete_artifact'), \
             patch('app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay') as callback:
            result = run_extraction_task.run('expired')
        self.assertEqual(result['status'], 'failed')
        self.assertIn('tiempo máximo', job['error'])
        provider.assert_not_called()
        callback.assert_called_once_with('expired')
        self.assertFalse(store.locks)

    def test_daily_quota_wait_cannot_postpone_expiration_for_a_day(self):
        now = datetime(2026, 11, 24, 17, tzinfo=timezone.utc)
        job = {'first_started_at': (now-timedelta(minutes=10)).isoformat(),
               'last_progress_at': (now-timedelta(minutes=10)).isoformat()}
        with patch('app.services.cv_queue.utc_now', return_value=now):
            self.assertEqual(retry_delay(job, 86400), 300)
            self.assertEqual(remaining_seconds(job), 300)
        job['last_progress_at'] = now.isoformat()
        with patch('app.services.cv_queue.utc_now', return_value=now):
            self.assertEqual(retry_delay(job, 86400), 900)

    def test_total_window_is_not_reset_by_regular_progress(self):
        now = datetime(2026, 11, 24, 17, tzinfo=timezone.utc)
        job = {'first_started_at': (now-timedelta(minutes=31)).isoformat(), 'last_progress_at': now.isoformat()}
        with patch('app.services.cv_queue.utc_now', return_value=now):
            self.assertLess(remaining_seconds(job), 0)

    def test_api_progress_counts_leaves_and_never_exposes_facts(self):
        now = datetime(2026, 11, 24, 17, tzinfo=timezone.utc)
        job = {'status': 'rate_limited', 'wait_reason': 'provider_quota',
               'next_attempt_at': (now+timedelta(seconds=12)).isoformat(),
               'extraction_packets': {'packets': {'parent': {'status': 'split'},
                   'a': {'status': 'completed', 'result': {'private': 'source quote'}}, 'b': {'status': 'pending'}}}}
        with patch('app.services.cv_queue.utc_now', return_value=now):
            progress = public_progress(job)
        self.assertEqual((progress['completed_packets'], progress['total_packets']), (1, 2))
        self.assertEqual(progress['retry_after_seconds'], 12)
        self.assertNotIn('source quote', str(progress))

    def test_extraction_turn_has_soft_and_hard_worker_limits(self):
        options = run_extraction_task._get_exec_options()
        self.assertEqual(options['soft_time_limit'], 240)
        self.assertEqual(options['time_limit'], 300)
        self.assertFalse(run_extraction_task.acks_on_failure_or_timeout)
