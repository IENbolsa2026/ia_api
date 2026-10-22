"""Recovery must preserve committed results and the worker's lease ownership."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from celery.exceptions import Retry

from app.api.v1.endpoints.cv_analyses import _submit, retrieve_cv_analysis
from app.services.job_store import LockOwnershipLost
from app.services.match_job_store import MatchJobStore
from app.tasks.cv_analysis_tasks import deliver_extraction_result_task, run_extraction_task
from app.tasks.match_tasks import evaluate_match_task
from ien_contracts.utils import content_hash


class RecoveryIntegrityTests(unittest.TestCase):
    def test_callback_enqueue_failure_still_retries_after_many_local_quota_waits(self):
        state = {
            'job_id': 'synthetic-delivery', 'status': 'accepted',
            'payload': {'kind': 'profile_normalization', 'candidate_id': 42, 'profile_snapshot': {}},
        }
        store = MagicMock()
        store.get.side_effect = lambda _job_id: state.copy()
        store.acquire_lock.return_value = True

        def update_owned(_job_id, **changes):
            state.update(changes)
            return state.copy()

        store.update_owned.side_effect = update_owned
        with patch('app.tasks.cv_analysis_tasks.ExtractionJobStore', return_value=store), \
             patch('app.tasks.cv_analysis_tasks.normalize_candidate_profile', new_callable=AsyncMock, return_value={}), \
             patch('app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay', side_effect=OSError('synthetic broker unavailable')):
            run_extraction_task.push_request(
                id='synthetic-extraction', retries=50, called_directly=False, is_eager=True,
                args=('synthetic-delivery',), kwargs={},
            )
            try:
                # Exercise Celery's real retry limit instead of mocking retry().
                with self.assertRaises(Retry):
                    run_extraction_task.run('synthetic-delivery')
            finally:
                run_extraction_task.pop_request()
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['result'], {})
        self.assertEqual(state['delivery_enqueue_failures'], 1)

    def test_callback_enqueue_failures_remain_bounded_without_repeating_inference(self):
        state = {
            'job_id': 'synthetic-terminal', 'status': 'completed', 'result': {'skills': []},
            'error': None, 'delivery_enqueue_failures': 4,
        }
        store = MagicMock()
        store.get.side_effect = lambda _job_id: state.copy()
        store.update.side_effect = lambda _job_id, **changes: state.update(changes)
        with patch('app.tasks.cv_analysis_tasks.ExtractionJobStore', return_value=store), \
             patch('app.tasks.cv_analysis_tasks.process_cv_text') as inference, \
             patch('app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay', side_effect=OSError('synthetic broker unavailable')):
            with self.assertRaises(OSError):
                run_extraction_task.run('synthetic-terminal')
        self.assertEqual(state['status'], 'delivery_failed')
        self.assertEqual(state['delivery_enqueue_failures'], 5)
        self.assertEqual(state['result'], {'skills': []})
        self.assertIsNone(state['error'])
        inference.assert_not_called()
        store.acquire_lock.assert_not_called()

    def test_idempotent_workers_keep_messages_until_completion(self):
        for task in (run_extraction_task, evaluate_match_task, deliver_extraction_result_task):
            with self.subTest(task=task.name):
                self.assertTrue(task.acks_late)
                self.assertTrue(task.reject_on_worker_lost)

    def test_cv_delivery_failure_requeues_delivery_without_reextracting(self):
        store = MagicMock()
        store.get.return_value = {
            "job_id": "cv-recovery", "status": "delivery_failed", "result": {"skills": []},
            "payload": {"kind": "candidate_cv"},
        }
        with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
             patch("app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay") as delivery, \
             patch("app.tasks.cv_analysis_tasks.process_cv_text") as extraction:
            result = run_extraction_task.run("cv-recovery")
        self.assertEqual(result["status"], "delivery_failed")
        extraction.assert_not_called()
        delivery.assert_called_once_with("cv-recovery")
        store.acquire_lock.assert_not_called()

    def test_premature_callback_cannot_consume_an_active_extraction(self):
        store = MagicMock()
        store.get.return_value = {"job_id": "cv-active", "status": "processing"}
        with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
             patch("app.tasks.cv_analysis_tasks.notify_extraction_result") as notify:
            result = deliver_extraction_result_task.run("cv-active")
        self.assertEqual(result["status"], "processing")
        notify.assert_not_called()
        store.update.assert_not_called()

    def test_callback_exhaustion_preserves_the_original_extraction_error(self):
        job = {
            "job_id": "cv-failed", "idempotency_key": "request-1", "status": "failed",
            "result": None, "error": "El archivo no contiene texto.",
            "payload": {"kind": "candidate_cv", "source_hash": "a" * 64},
        }
        store = MagicMock()
        store.get.return_value = job
        with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
             patch("app.tasks.cv_analysis_tasks.notify_extraction_result", side_effect=OSError("unavailable")):
            deliver_extraction_result_task.push_request(retries=8)
            try:
                with self.assertRaises(OSError):
                    deliver_extraction_result_task.run("cv-failed")
            finally:
                deliver_extraction_result_task.pop_request()
        terminal = store.update.call_args.kwargs
        self.assertEqual(terminal["status"], "delivery_failed")
        self.assertNotIn("error", terminal)
        self.assertIn("delivery_error", terminal)

    def test_cv_public_status_tracks_result_for_all_delivery_states(self):
        for state in ("delivering", "delivered", "delivery_failed"):
            for result in (None, {}):
                with self.subTest(state=state, result=result):
                    expected = "failed" if result is None else "completed"
                    job = {"job_id": "cv-status", "status": state, "result": result}
                    store = MagicMock()
                    store.get.return_value = job
                    store.create_or_get.return_value = (job, False)
                    with patch("app.api.v1.endpoints.cv_analyses.ExtractionJobStore", return_value=store), \
                         patch("app.api.v1.endpoints.cv_analyses.celery_app.send_task") as enqueue:
                        status = asyncio.run(retrieve_cv_analysis("cv-status"))
                        accepted = _submit("request", "digest", {})
                    self.assertEqual(status.status.value, expected)
                    self.assertEqual(status.delivered, state == "delivered")
                    self.assertEqual(accepted.status.value, expected)
                    enqueue.assert_not_called()

    def test_match_refreshes_after_lock_without_respending_quota(self):
        store = MagicMock()
        store.get.side_effect = [
            {"status": "processing"},
            {"status": "completed", "result": {"match_percentage": 80}},
        ]
        store.acquire_lock.return_value = True
        with patch("app.tasks.match_tasks.MatchJobStore", return_value=store), \
             patch("app.services.match_groq.groq_rate_limiter") as limiter, \
             patch("app.tasks.match_tasks.GroqClient") as provider:
            result = evaluate_match_task.run("match-completed")
        self.assertEqual(result, {"match_percentage": 80})
        limiter.assert_not_called()
        provider.assert_not_called()
        store.update_owned.assert_not_called()
        store.release_lock.assert_called_once_with("match-completed")

    def test_match_lost_lease_retries_without_overwriting_state(self):
        store = MagicMock()
        store.get.return_value = {"status": "accepted"}
        store.acquire_lock.return_value = True
        store.update_owned.side_effect = LockOwnershipLost("expired")
        with patch("app.tasks.match_tasks.MatchJobStore", return_value=store), \
             patch("app.services.match_groq.groq_rate_limiter") as limiter, \
             patch.object(evaluate_match_task, "retry", side_effect=Retry("deferred")) as retry:
            with self.assertRaises(Retry):
                evaluate_match_task.run("match-stale")
        limiter.assert_not_called()
        store.update.assert_not_called()
        self.assertEqual(retry.call_args.kwargs["countdown"], 30)
        store.release_lock.assert_called_once_with("match-stale")

    def test_match_store_fences_writes_and_release_with_the_owner_token(self):
        client = MagicMock()
        client.set.return_value = True
        client.get.return_value = json.dumps({"status": "processing", "items": []})
        store = MatchJobStore(client=client)
        self.assertTrue(store.acquire_lock("match-lease"))
        token = client.set.call_args.args[1]
        self.assertNotEqual(token, "1")
        client.eval.return_value = 0
        with self.assertRaises(LockOwnershipLost):
            store.update_owned("match-lease", status="completed")
        self.assertEqual(client.eval.call_args.args[4], token)
        store.release_lock("match-lease")
        self.assertEqual(client.eval.call_args.args[3], token)
        client.delete.assert_not_called()

    def test_concurrent_request_without_mapping_cannot_claim_another_payload(self):
        request = {"request_id": "same-id", "candidate_profile": {"profile_id": "1"}}
        existing = {"request_id": "same-id", "request_hash": content_hash({**request, "candidate_profile": {"profile_id": "2"}})}
        client = MagicMock()
        client.get.side_effect = [None, json.dumps(existing)]
        client.set.return_value = False
        store = MatchJobStore(client=client)
        with self.assertRaises(ValueError):
            store.create_or_get(request)
        self.assertEqual(client.set.call_count, 1)
