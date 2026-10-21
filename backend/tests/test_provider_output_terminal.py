"""Un exceso permanente de tokens de salida debe fallar una vez y entregarse."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.rate_limiter import ProviderOutputLimitExceeded, ProviderResponseTruncated
from app.tasks.cv_analysis_tasks import deliver_extraction_result_task, run_extraction_task


class InMemoryExtractionStore:
    def __init__(self, job):
        self.job = job
        self.updates = []

    def get(self, job_id):
        return self.job

    def acquire_lock(self, job_id):
        return True

    def update(self, job_id, **changes):
        self.updates.append(changes.copy())
        self.job.update(changes)
        return self.job

    def update_owned(self, job_id, **changes):
        return self.update(job_id, **changes)

    def release_lock(self, job_id):
        pass


class ProviderOutputTerminalTests(unittest.TestCase):
    def test_cv_output_overflow_is_failed_delivered_and_artifact_removed_without_retry(self):
        source = b"Synthetic resume"
        job = {
            "job_id": "job-output-limit",
            "idempotency_key": "cv-output-limit",
            "status": "accepted",
            "result": None,
            "error": None,
            "payload": {
                "kind": "candidate_cv",
                "analysis_id": 17,
                "candidate_id": 4,
                "user_id": None,
                "source_hash": hashlib.sha256(source).hexdigest(),
                "artifact_id": "artifact-output-limit",
                "format": "txt",
            },
        }
        store = InMemoryExtractionStore(job)
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "resume.txt"
            artifact.write_bytes(source)
            with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
                 patch("app.tasks.cv_analysis_tasks.artifact_path", return_value=artifact), \
                 patch("app.tasks.cv_analysis_tasks.extract_in_subprocess", return_value="Synthetic resume"), \
                 patch("app.tasks.cv_analysis_tasks.process_cv_text", new_callable=AsyncMock,
                       side_effect=ProviderOutputLimitExceeded(1000)), \
                 patch("app.tasks.cv_analysis_tasks.delete_artifact") as delete_artifact, \
                 patch("app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay") as enqueue_delivery, \
                 patch.object(run_extraction_task, "retry", side_effect=AssertionError("no retry expected")):
                result = run_extraction_task.run("job-output-limit")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(job["status"], "failed")
        self.assertIn("capacidad actual", job["error"])
        self.assertNotIn("provider", job["error"].lower())
        delete_artifact.assert_called_once_with("artifact-output-limit", "txt")
        enqueue_delivery.assert_called_once_with("job-output-limit")

        # Ejecuta el callback con el resultado terminal ya guardado: no debe
        # revelar mensajes/cuerpo del proveedor y sí comunicar el fallo seguro.
        with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
             patch("app.tasks.cv_analysis_tasks.notify_extraction_result") as notify:
            delivered = deliver_extraction_result_task.run("job-output-limit")
        self.assertEqual(delivered["status"], "delivered")
        callback = notify.call_args.args[0]
        self.assertEqual(callback["status"], "failed")
        self.assertIn("capacidad actual", callback["error"])
        self.assertNotIn("Request too large", callback["error"])

    def test_cv_truncated_provider_response_is_terminal_without_retry(self):
        source = b"Synthetic resume"
        job = {
            "job_id": "job-truncated",
            "idempotency_key": "cv-truncated",
            "status": "accepted",
            "result": None,
            "error": None,
            "payload": {
                "kind": "candidate_cv", "analysis_id": 18, "candidate_id": 4, "user_id": None,
                "source_hash": hashlib.sha256(source).hexdigest(), "artifact_id": "artifact-truncated", "format": "txt",
            },
        }
        store = InMemoryExtractionStore(job)
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "resume.txt"
            artifact.write_bytes(source)
            with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
                 patch("app.tasks.cv_analysis_tasks.artifact_path", return_value=artifact), \
                 patch("app.tasks.cv_analysis_tasks.extract_in_subprocess", return_value="Synthetic resume"), \
                 patch("app.tasks.cv_analysis_tasks.process_cv_text", new_callable=AsyncMock,
                       side_effect=ProviderResponseTruncated("private provider detail")), \
                 patch("app.tasks.cv_analysis_tasks.delete_artifact") as delete_artifact, \
                 patch("app.tasks.cv_analysis_tasks.deliver_extraction_result_task.delay") as enqueue_delivery, \
                 patch.object(run_extraction_task, "retry", side_effect=AssertionError("truncation is terminal")):
                result = run_extraction_task.run("job-truncated")

        self.assertEqual(result, {"job_id": "job-truncated", "status": "failed"})
        self.assertEqual(job["status"], "failed")
        self.assertIn("respuesta", job["error"])
        self.assertNotIn("private provider detail", job["error"])
        delete_artifact.assert_called_once_with("artifact-truncated", "txt")
        enqueue_delivery.assert_called_once_with("job-truncated")

        with patch("app.tasks.cv_analysis_tasks.ExtractionJobStore", return_value=store), \
             patch("app.tasks.cv_analysis_tasks.notify_extraction_result") as notify:
            deliver_extraction_result_task.run("job-truncated")
        callback = notify.call_args.args[0]
        self.assertEqual(callback["status"], "failed")
        self.assertNotIn("private provider detail", callback["error"])


if __name__ == "__main__":
    unittest.main()
