"""Una redelivery no debe confirmar un trabajo mientras siga vigente su lock."""

import unittest
import json
import hashlib
import tempfile
from unittest.mock import MagicMock, call, patch

from celery.exceptions import Retry

from app.tasks.cv_analysis_tasks import run_extraction_task
from app.tasks.match_tasks import evaluate_match_task
from app.services.job_store import ExtractionJobStore, LockOwnershipLost


class TaskLockRetryTests(unittest.TestCase):
    def test_cv_packet_progress_resets_rate_limit_stall_counter(self):
        from pathlib import Path
        from app.services.rate_limiter import RateLimitExceeded

        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "synthetic.pdf"
            artifact.write_bytes(b"synthetic")
            source_hash = hashlib.sha256(b"synthetic").hexdigest()
            job = {
                "job_id": "job-packets", "status": "accepted", "rate_limit_stalls": 4,
                "payload": {"kind": "candidate_cv", "analysis_id": 10, "candidate_id": 20,
                            "artifact_id": "artifact-id", "format": "pdf", "source_hash": source_hash},
            }
            latest = {**job, "extraction_packets": {"packets": {"packet": {"status": "completed"}}}}
            store = MagicMock()
            store.get.side_effect = [job, job, latest]
            store.acquire_lock.return_value = True
            store.renew_lock.return_value = True

            with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
                 patch("app.tasks.cv_analysis_tasks.artifact_path", return_value=artifact), \
                 patch("app.tasks.cv_analysis_tasks.extract_in_subprocess", return_value="synthetic text"), \
                 patch("app.tasks.cv_analysis_tasks.process_cv_text", side_effect=RateLimitExceeded(1)), \
                 patch.object(run_extraction_task, "retry", side_effect=Retry("deferred")) as retry:
                with self.assertRaises(Retry):
                    run_extraction_task.run("job-packets")

        self.assertEqual(store.update_owned.call_args.kwargs["rate_limit_stalls"], 0)
        self.assertEqual(retry.call_args.kwargs["max_retries"], 1000)
        store.release_lock.assert_has_calls([call("job-packets"), call("__cv_serial__")])
        self.assertEqual(store.release_lock.call_count, 2)

    def test_cv_worker_refreshes_terminal_state_after_lock_and_skips_inference(self):
        original = {"job_id": "job-race", "status": "accepted", "payload": {"kind": "candidate_cv"}}
        completed = {
            "job_id": "job-race", "status": "completed", "result": {"skills": [{"source_quotes": ["private quote"]}]},
            "payload": {"kind": "candidate_cv"},
        }
        store = MagicMock()
        store.get.side_effect = [original, completed]
        store.acquire_lock.return_value = True

        with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
             patch("app.tasks.cv_analysis_tasks.process_cv_text") as process_cv, \
             patch("app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay") as delivery:
            result = run_extraction_task.run("job-race")

        process_cv.assert_not_called()
        delivery.assert_called_once_with("job-race")
        store.update_owned.assert_not_called()
        self.assertEqual(result, {"job_id": "job-race", "status": "completed"})
        self.assertNotIn("source_quotes", str(result))
        store.release_lock.assert_has_calls([call("job-race"), call("__cv_serial__")])
        self.assertEqual(store.release_lock.call_count, 2)

    def test_completed_worker_return_does_not_expose_profile(self):
        state = {
            "job_id": "job-private", "status": "accepted",
            "payload": {"kind": "profile_normalization", "candidate_id": 42, "profile_snapshot": {}},
        }
        store = MagicMock()
        store.get.side_effect = lambda _job_id: state.copy()
        store.acquire_lock.return_value = True

        def update_owned(_job_id, **changes):
            state.update(changes)
            return state.copy()

        store.update_owned.side_effect = update_owned
        profile_with_evidence = {"skills": [{"source_quotes": ["private source quote"]}]}
        async def normalize(*_args):
            return profile_with_evidence

        with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
             patch("app.tasks.cv_analysis_tasks.normalize_candidate_profile", side_effect=normalize), \
             patch("app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay"):
            result = run_extraction_task.run("job-private")

        self.assertEqual(result, {"job_id": "job-private", "status": "completed"})
        self.assertEqual(state["result"], profile_with_evidence)
        self.assertNotIn("source_quotes", str(result))

    def test_callback_queue_failure_cannot_degrade_completed_result(self):
        state = {
            "job_id": "job-callback", "status": "accepted",
            "payload": {"kind": "profile_normalization", "candidate_id": 42, "profile_snapshot": {}},
        }
        store = MagicMock()
        store.get.side_effect = lambda _job_id: state.copy()
        store.acquire_lock.return_value = True

        def update_owned(_job_id, **changes):
            state.update(changes)
            return state.copy()

        store.update_owned.side_effect = update_owned

        async def normalize(*_args):
            return {"skills": [{"source_quotes": ["synthetic quote"]}]}

        with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
             patch("app.tasks.cv_analysis_tasks.normalize_candidate_profile", side_effect=normalize), \
             patch("app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay", side_effect=OSError("broker unavailable")), \
             patch.object(run_extraction_task, "retry", side_effect=OSError("retry exhausted")):
            with self.assertRaisesRegex(OSError, "retry exhausted"):
                run_extraction_task.run("job-callback")

        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["result"]["skills"][0]["source_quotes"], ["synthetic quote"])

    def test_owned_job_update_is_atomic_and_preserves_empty_arrays(self):
        client = MagicMock()
        client.get.return_value = json.dumps({"job_id": "job-1", "status": "processing", "items": []})
        client.eval.return_value = 1
        store = ExtractionJobStore(client=client)
        store._lock_tokens["job-1"] = "owner-token"

        saved = store.update_owned("job-1", extraction_progress={"pages": []})

        self.assertEqual(saved["items"], [])
        self.assertEqual(saved["extraction_progress"]["pages"], [])
        args = client.eval.call_args.args
        self.assertEqual(args[1], 2)
        self.assertEqual(args[-2], 600)
        encoded_job = args[-1]
        self.assertEqual(json.loads(encoded_job)["items"], [])

    def test_owned_job_update_fails_if_token_was_replaced(self):
        client = MagicMock()
        client.get.return_value = json.dumps({"job_id": "job-1", "status": "processing"})
        client.eval.return_value = 0
        store = ExtractionJobStore(client=client)
        store._lock_tokens["job-1"] = "stale-token"

        with self.assertRaises(LockOwnershipLost):
            store.update_owned("job-1", status="completed")

    def test_cv_task_retries_with_backoff_when_lock_is_occupied(self):
        store = MagicMock()
        store.get.return_value = {"job_id": "job-1", "status": "accepted"}
        store.acquire_lock.return_value = False

        with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
             patch.object(run_extraction_task, "retry", side_effect=Retry("deferred")) as retry:
            with self.assertRaises(Retry):
                run_extraction_task.run("job-1")

        self.assertEqual(retry.call_args.kwargs["countdown"], 30)
        store.update.assert_not_called()
        store.release_lock.assert_not_called()

    def test_match_task_retries_before_reserving_provider_quota_when_lock_is_occupied(self):
        store = MagicMock()
        store.get.return_value = {"request_id": "request-1", "status": "accepted", "request": {}}
        store.acquire_lock.return_value = False

        with patch("app.tasks.match_tasks.MatchJobStore", return_value=store), \
             patch("app.services.match_groq.groq_rate_limiter") as limiter, \
             patch.object(evaluate_match_task, "retry", side_effect=Retry("deferred")) as retry:
            with self.assertRaises(Retry):
                evaluate_match_task.run("request-1")

        self.assertEqual(retry.call_args.kwargs["countdown"], 30)
        limiter.assert_not_called()
        store.update.assert_not_called()
        store.release_lock.assert_not_called()
