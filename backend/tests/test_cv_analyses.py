"""Pruebas del contrato HTTP para el ingreso de CVs."""

import unittest
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings
from app.api.v1.endpoints.cv_analyses import _submit
from ien_contracts.utils import content_hash


class CreateCVAnalysisTests(unittest.TestCase):
    """La ruta debe validar antes de enviar trabajo a Celery."""

    def setUp(self) -> None:
        self.client = TestClient(app)
        self.form_data = {"analysis_id": "10", "candidate_id": "20"}
        self.pdf_bytes = b"%PDF-1.4\nCV de prueba\n%%EOF"
        self.headers = {"Authorization": "Bearer service-token", "Idempotency-Key": "cv-test-key-123"}
        self.token_patch = patch.object(settings, "SERVICE_API_TOKEN", "service-token")
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.artifact_temp = tempfile.TemporaryDirectory()
        self.artifact_patch = patch.object(settings, "CV_ARTIFACT_DIR", self.artifact_temp.name)
        self.artifact_patch.start()
        self.addCleanup(self.artifact_patch.stop)
        self.addCleanup(self.artifact_temp.cleanup)

    def post_cv(self, content: bytes, content_type: str = "application/pdf"):
        return self.client.post(
            "/api/v1/cv-analyses",
            data=self.form_data,
            files={"cv": ("cv.pdf", content, content_type)},
            headers=self.headers,
        )

    @patch("app.api.v1.endpoints.cv_analyses.celery_app.send_task")
    @patch("app.api.v1.endpoints.cv_analyses.ExtractionJobStore")
    def test_valid_pdf_is_queued_in_celery(self, store_cls, send_task) -> None:
        """Un PDF válido se serializa y Celery devuelve el identificador."""
        store_cls.return_value.create_or_get.return_value = ({"job_id": "job-123", "status": "accepted"}, True)

        response = self.post_cv(self.pdf_bytes)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job_id"], "job-123")
        self.assertEqual(response.json()["status"], "accepted")
        self.assertEqual(response.json()["request_id"], "cv-test-key-123")
        send_task.assert_called_once()

    @patch("app.api.v1.endpoints.cv_analyses.celery_app.send_task")
    @patch("app.api.v1.endpoints.cv_analyses.ExtractionJobStore")
    def test_optional_cv_version_is_persisted_only_when_supplied(self, store_cls, _send_task) -> None:
        store_cls.return_value.create_or_get.return_value = ({"job_id": "job-version", "status": "accepted"}, True)
        response = self.client.post(
            "/api/v1/cv-analyses",
            data={**self.form_data, "cv_version_id": "31"},
            files={"cv": ("cv.pdf", self.pdf_bytes, "application/pdf")},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202)
        payload = store_cls.return_value.create_or_get.call_args.args[2]
        self.assertEqual(payload["cv_version_id"], 31)

        store_cls.return_value.create_or_get.reset_mock()
        response = self.post_cv(self.pdf_bytes)
        self.assertEqual(response.status_code, 202)
        payload = store_cls.return_value.create_or_get.call_args.args[2]
        self.assertNotIn("cv_version_id", payload)

    @patch("app.api.v1.endpoints.cv_analyses.analyze_cv_task.delay")
    def test_non_pdf_is_rejected_without_queueing(self, delay_mock) -> None:
        """Un archivo con tipo distinto a PDF no llega al worker."""

        response = self.post_cv(b"texto plano", "text/plain")

        self.assertEqual(response.status_code, 415)
        self.assertIn("no coincide", response.json()["detail"])
        delay_mock.assert_not_called()

    @patch("app.api.v1.endpoints.cv_analyses.analyze_cv_task.delay")
    def test_empty_pdf_is_rejected_without_queueing(self, delay_mock) -> None:
        """El archivo vacío se rechaza antes de ocupar la cola."""

        response = self.post_cv(b"")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "El archivo está vacío.")
        delay_mock.assert_not_called()

    @patch("app.api.v1.endpoints.cv_analyses.analyze_cv_task.delay")
    def test_oversized_pdf_is_rejected_without_queueing(self, delay_mock) -> None:
        """El límite de 5 MB se aplica antes de publicar en Celery."""

        response = self.post_cv(b"x" * (settings.CV_MAX_PDF_BYTES + 1))

        self.assertEqual(response.status_code, 413)
        self.assertIn("máximo permitido", response.json()["detail"])
        delay_mock.assert_not_called()

    @patch("app.api.v1.endpoints.cv_analyses.celery_app.send_task")
    @patch("app.api.v1.endpoints.cv_analyses.ExtractionJobStore")
    def test_txt_is_queued_even_when_client_claims_pdf_mime(self, store_cls, send_task) -> None:
        store_cls.return_value.create_or_get.return_value = ({"job_id": "job-txt", "status": "accepted"}, True)
        response = self.client.post(
            "/api/v1/cv-analyses", data=self.form_data,
            files={"cv": ("cv.txt", "Experiencia como chofer".encode(), "application/pdf")},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202)
        stored = store_cls.return_value.create_or_get.call_args.args[2]
        self.assertEqual(stored["format"], "txt")
        self.assertEqual(stored["source_hash"], __import__("hashlib").sha256(b"Experiencia como chofer").hexdigest())
        send_task.assert_called_once()

    @patch("app.api.v1.endpoints.cv_analyses.celery_app.send_task")
    @patch("app.api.v1.endpoints.cv_analyses.ExtractionJobStore")
    def test_requeued_accepted_job_uses_the_existing_job_id(self, store_cls, send_task) -> None:
        store_cls.return_value.create_or_get.return_value = ({"job_id": "original-job", "status": "accepted"}, False)
        accepted = _submit("same-key", "a" * 64, {"kind": "candidate_cv"})
        self.assertEqual(accepted.job_id, "original-job")
        self.assertEqual(send_task.call_args.kwargs["args"], ["original-job"])

    @patch("app.api.v1.endpoints.cv_analyses.celery_app.send_task")
    @patch("app.api.v1.endpoints.cv_analyses.ExtractionJobStore")
    def test_profile_normalization_is_queued_with_service_auth(self, store_cls, send_task) -> None:
        store_cls.return_value.create_or_get.return_value = ({"job_id": "job-normalize", "status": "accepted"}, True)
        snapshot = {"schema_version": "candidate-profile.v1", "profile_id": "20"}

        response = self.client.post(
            "/api/v1/profile-normalizations",
            json={
                "candidate_id": 20,
                "source_hash": content_hash(snapshot),
                "profile_snapshot": snapshot,
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job_id"], "job-normalize")
        payload = store_cls.return_value.create_or_get.call_args.args[2]
        self.assertEqual(payload["kind"], "profile_normalization")
        send_task.assert_called_once()

    @patch("app.api.v1.endpoints.cv_analyses._submit")
    def test_offer_and_profile_reject_source_hash_that_does_not_match_snapshot(self, submit) -> None:
        offer = self.client.post(
            "/api/v1/offer-analyses",
            json={"offer_id": 3, "source_hash": "a" * 64, "offer_snapshot": {"title": "Chofer"}},
            headers=self.headers,
        )
        profile = self.client.post(
            "/api/v1/profile-normalizations",
            json={"candidate_id": 20, "source_hash": "b" * 64, "profile_snapshot": {"profile_id": "20"}},
            headers=self.headers,
        )
        self.assertEqual(offer.status_code, 422)
        self.assertEqual(profile.status_code, 422)
        self.assertIn("no coincide", offer.json()["detail"])
        self.assertIn("no coincide", profile.json()["detail"])
        submit.assert_not_called()

    def test_same_snapshot_hash_with_different_owner_conflicts_on_same_key(self) -> None:
        jobs = {}

        class IdempotentStore:
            def create_or_get(self, key, payload_hash, payload, job_id):
                if key in jobs:
                    existing_hash, existing_job = jobs[key]
                    if existing_hash != payload_hash:
                        raise ValueError("idempotency conflict")
                    return existing_job, False
                job = {"job_id": job_id, "status": "accepted"}
                jobs[key] = (payload_hash, job)
                return job, True

        snapshot = {"title": "Chofer", "requirements": []}
        source_hash = content_hash(snapshot)
        offer_headers = {**self.headers, "Idempotency-Key": "same-offer-key"}
        profile_headers = {**self.headers, "Idempotency-Key": "same-profile-key"}
        with patch("app.api.v1.endpoints.cv_analyses.ExtractionJobStore", IdempotentStore), \
             patch("app.api.v1.endpoints.cv_analyses.celery_app.send_task"):
            offer_first = self.client.post(
                "/api/v1/offer-analyses",
                json={"offer_id": 3, "source_hash": source_hash, "offer_snapshot": snapshot},
                headers=offer_headers,
            )
            offer_second = self.client.post(
                "/api/v1/offer-analyses",
                json={"offer_id": 4, "source_hash": source_hash, "offer_snapshot": snapshot},
                headers=offer_headers,
            )
            profile_first = self.client.post(
                "/api/v1/profile-normalizations",
                json={"candidate_id": 20, "source_hash": source_hash, "profile_snapshot": snapshot},
                headers=profile_headers,
            )
            profile_second = self.client.post(
                "/api/v1/profile-normalizations",
                json={"candidate_id": 21, "source_hash": source_hash, "profile_snapshot": snapshot},
                headers=profile_headers,
            )
        self.assertEqual(offer_first.status_code, 202)
        self.assertEqual(offer_second.status_code, 409)
        self.assertIn("ya se usó", offer_second.json()["detail"])
        self.assertEqual(profile_first.status_code, 202)
        self.assertEqual(profile_second.status_code, 409)
        self.assertIn("ya se usó", profile_second.json()["detail"])
