from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from celery.exceptions import Retry

from app.core.celery_app import celery_app
from app.services.job_store import LockOwnershipLost
from app.tasks.cv_analysis_tasks import CV_SERIAL_LOCK_ID, run_extraction_task


def cv_job(job_id: str) -> dict:
    data = b"synthetic CV artifact"
    return {
        "job_id": job_id,
        "status": "accepted",
        "payload": {
            "kind": "candidate_cv",
            "analysis_id": 11,
            "candidate_id": 22,
            "artifact_id": f"artifact-{job_id}",
            "format": "pdf",
            "source_hash": hashlib.sha256(data).hexdigest(),
        },
    }


class SharedLockStore:
    def __init__(
        self,
        jobs: dict[str, dict],
        *,
        lose_serial_renewal: bool = False,
        unavailable_locks: set[str] | None = None,
    ):
        self.jobs = jobs
        self.locks: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        self.releases: list[str] = []
        self.unavailable_locks = unavailable_locks or set()
        self.lose_serial_renewal = lose_serial_renewal

    def get(self, job_id):
        job = self.jobs.get(job_id)
        return dict(job) if job else None

    def acquire_lock(self, lock_id):
        if lock_id in self.locks or lock_id in self.unavailable_locks:
            return False
        token = f"token-{lock_id}"
        self.locks[lock_id] = token
        self.tokens[lock_id] = token
        return True

    def renew_lock(self, lock_id):
        if self.lose_serial_renewal and lock_id == CV_SERIAL_LOCK_ID:
            self.locks[lock_id] = "new-owner-token"
            return False
        return self.locks.get(lock_id) == self.tokens.get(lock_id)

    def release_lock(self, lock_id):
        self.releases.append(lock_id)
        if self.locks.get(lock_id) == self.tokens.get(lock_id):
            del self.locks[lock_id]
        self.tokens.pop(lock_id, None)

    def update_owned(self, job_id, **changes):
        if self.locks.get(job_id) != self.tokens.get(job_id):
            raise LockOwnershipLost("stale job lease")
        self.jobs[job_id].update(changes)
        return dict(self.jobs[job_id])


class CvSerialQueueTests(unittest.TestCase):
    def test_second_cv_requeues_while_first_holds_global_slot(self):
        jobs = {"cv-first": cv_job("cv-first"), "cv-second": cv_job("cv-second")}
        store = SharedLockStore(jobs)
        retry_calls = []
        nested_deferred = []
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            artifact = Path(folder) / "synthetic.pdf"
            artifact.write_bytes(b"synthetic CV artifact")

            def retry(*args, **kwargs):
                retry_calls.append(kwargs)
                raise Retry("deferred")

            async def process_cv(_text, _candidate_id, *, on_heartbeat, **_kwargs):
                try:
                    run_extraction_task.run("cv-second")
                except Retry:
                    nested_deferred.append(True)
                on_heartbeat()
                return {"schema_version": "candidate-profile.v2"}

            with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
                 patch("app.tasks.cv_analysis_tasks.artifact_path", return_value=artifact), \
                 patch("app.tasks.cv_analysis_tasks.extract_in_subprocess", return_value="synthetic text"), \
                 patch("app.tasks.cv_analysis_tasks.process_cv_text", side_effect=process_cv) as process, \
                 patch("app.tasks.cv_analysis_tasks.delete_artifact"), \
                 patch("app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay"), \
                 patch.object(run_extraction_task, "retry", side_effect=retry):
                result = run_extraction_task.run("cv-first")

        self.assertEqual(result, {"job_id": "cv-first", "status": "completed"})
        self.assertEqual(process.call_count, 1)
        self.assertEqual(nested_deferred, [True])
        self.assertEqual(retry_calls[0]["countdown"], 5)
        self.assertGreaterEqual(retry_calls[0]["max_retries"], 1)
        self.assertEqual(jobs["cv-second"]["status"], "accepted")
        self.assertEqual(jobs["cv-first"]["status"], "completed")
        self.assertEqual(store.releases, ["cv-first", CV_SERIAL_LOCK_ID])
        self.assertNotIn(CV_SERIAL_LOCK_ID, store.locks)

    def test_lost_global_lease_aborts_cv_without_publishing_result(self):
        jobs = {"cv-stale": cv_job("cv-stale")}
        store = SharedLockStore(jobs, lose_serial_renewal=True)
        retry_calls = []
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            artifact = Path(folder) / "synthetic.pdf"
            artifact.write_bytes(b"synthetic CV artifact")

            def retry(*args, **kwargs):
                retry_calls.append(kwargs)
                raise Retry("deferred")

            async def process_cv(_text, _candidate_id, *, on_heartbeat, **_kwargs):
                on_heartbeat()
                self.fail("A lost global lease must abort before returning a profile.")

            with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
                 patch("app.tasks.cv_analysis_tasks.artifact_path", return_value=artifact), \
                 patch("app.tasks.cv_analysis_tasks.extract_in_subprocess", return_value="synthetic text"), \
                 patch("app.tasks.cv_analysis_tasks.process_cv_text", side_effect=process_cv), \
                 patch("app.tasks.cv_analysis_tasks.delete_artifact"), \
                 patch("app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay") as delivery, \
                 patch.object(run_extraction_task, "retry", side_effect=retry):
                with self.assertRaises(Retry):
                    run_extraction_task.run("cv-stale")

        delivery.assert_not_called()
        self.assertEqual(retry_calls[-1]["countdown"], 30)
        self.assertEqual(jobs["cv-stale"]["status"], "processing")
        self.assertEqual(store.releases, ["cv-stale", CV_SERIAL_LOCK_ID])
        self.assertEqual(store.locks[CV_SERIAL_LOCK_ID], "new-owner-token")

    def test_job_lock_failure_releases_global_slot_before_retry(self):
        jobs = {"cv-job-locked": cv_job("cv-job-locked")}
        store = SharedLockStore(jobs, unavailable_locks={"cv-job-locked"})
        retry_calls = []

        def retry(*args, **kwargs):
            retry_calls.append(kwargs)
            raise Retry("deferred")

        with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
             patch("app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay") as delivery, \
             patch.object(run_extraction_task, "retry", side_effect=retry):
            with self.assertRaises(Retry):
                run_extraction_task.run("cv-job-locked")

        delivery.assert_not_called()
        self.assertEqual(retry_calls[0]["countdown"], 30)
        self.assertEqual(retry_calls[0]["max_retries"], 1000)
        self.assertEqual(store.releases, [CV_SERIAL_LOCK_ID])
        self.assertNotIn(CV_SERIAL_LOCK_ID, store.locks)

    def test_celery_uses_single_slot_and_minimal_prefetch(self):
        # A route alone loses to the default task priority in apply_async.
        from app.tasks.cv_analysis_tasks import deliver_extraction_result_task
        self.assertEqual(deliver_extraction_result_task._get_exec_options()['priority'], 0)
        self.assertEqual(celery_app.conf.worker_concurrency, 1)
        self.assertEqual(celery_app.conf.worker_prefetch_multiplier, 1)
        self.assertEqual(celery_app.conf.task_default_priority, 5)
        self.assertEqual(celery_app.conf.broker_transport_options["queue_order_strategy"], "priority")
        self.assertEqual(
            celery_app.conf.task_routes["app.tasks.deliver_extraction_result"]["priority"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
