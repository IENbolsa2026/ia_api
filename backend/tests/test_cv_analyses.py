"""Pruebas del contrato HTTP para el ingreso de CVs."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.core.config import settings


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

    @patch("app.api.v1.endpoints.cv_analyses.analyze_cv_task.delay")
    def test_non_pdf_is_rejected_without_queueing(self, delay_mock) -> None:
        """Un archivo con tipo distinto a PDF no llega al worker."""

        response = self.post_cv(b"texto plano", "text/plain")

        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.json()["detail"], "El CV debe enviarse en formato PDF.")
        delay_mock.assert_not_called()

    @patch("app.api.v1.endpoints.cv_analyses.analyze_cv_task.delay")
    def test_empty_pdf_is_rejected_without_queueing(self, delay_mock) -> None:
        """El archivo vacío se rechaza antes de ocupar la cola."""

        response = self.post_cv(b"")

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"], "El PDF está vacío.")
        delay_mock.assert_not_called()

    @patch("app.api.v1.endpoints.cv_analyses.analyze_cv_task.delay")
    def test_oversized_pdf_is_rejected_without_queueing(self, delay_mock) -> None:
        """El límite de 5 MB se aplica antes de publicar en Celery."""

        response = self.post_cv(b"x" * (settings.CV_MAX_PDF_BYTES + 1))

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"], "El PDF supera el máximo permitido.")
        delay_mock.assert_not_called()

    @patch("app.api.v1.endpoints.cv_analyses.celery_app.send_task")
    @patch("app.api.v1.endpoints.cv_analyses.ExtractionJobStore")
    def test_profile_normalization_is_queued_with_service_auth(self, store_cls, send_task) -> None:
        store_cls.return_value.create_or_get.return_value = ({"job_id": "job-normalize", "status": "accepted"}, True)

        response = self.client.post(
            "/api/v1/profile-normalizations",
            json={
                "candidate_id": 20,
                "source_hash": "a" * 64,
                "profile_snapshot": {"schema_version": "candidate-profile.v1", "profile_id": "20"},
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job_id"], "job-normalize")
        payload = store_cls.return_value.create_or_get.call_args.args[2]
        self.assertEqual(payload["kind"], "profile_normalization")
        send_task.assert_called_once()
