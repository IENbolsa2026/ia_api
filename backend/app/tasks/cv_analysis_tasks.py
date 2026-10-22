"""Tareas separadas para inferencia y entrega de resultados."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from typing import Any
from uuid import uuid4
from celery.exceptions import Retry

from app.core.celery_app import celery_app
from app.core.config import settings
from app.services.cv_pipeline import CV_EXTRACTION_PROMPT_VERSION, normalize_candidate_profile, process_cv_text, process_offer_snapshot
from app.services.cv_packets import PacketsIdentityError, count_completed_packets, make_packet_identity
from app.services.job_store import ExtractionJobStore, LockOwnershipLost
from app.services.document_text import DocumentRejected
from app.services.cv_artifacts import artifact_path, delete_artifact
from app.services.document_extractor_runner import extract_in_subprocess
from app.services.rate_limiter import ProviderOutputLimitExceeded, ProviderResponseTruncated, RateLimitExceeded
from app.services.gemini_client import GeminiUnavailable
from app.services.result_notifier import notify_extraction_result
from app.services.channel_events import publish_cv_analysis_complete


logger = logging.getLogger(__name__)
CV_SERIAL_LOCK_ID = "__cv_serial__"


def _callback_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = job["payload"]
    result = {
        "event_id": f"extraction-{job['job_id']}",
        "job_id": job["job_id"],
        "request_id": job["idempotency_key"],
        "analysis_id": payload.get("analysis_id"),
        "candidate_id": payload.get("candidate_id"),
        "offer_id": payload.get("offer_id"),
        "kind": payload["kind"],
        "source_hash": payload["source_hash"],
        "status": "completed" if job.get("result") is not None else "failed",
        "result": job.get("result"),
        "error": job.get("error"),
        "provider_model": job.get("provider_model"),
    }
    if "cv_version_id" in payload:
        result["cv_version_id"] = payload["cv_version_id"]
    return result


def _queue_result_delivery(task: Any, store: ExtractionJobStore, job_id: str, *, owned: bool = False) -> None:
    try:
        deliver_extraction_result_task.delay(job_id)
    except Exception as exc:
        job = store.get(job_id)
        if job is None:
            raise
        failures = int(job.get("delivery_enqueue_failures", 0)) + 1
        changes: dict[str, Any] = {"delivery_enqueue_failures": failures}
        if failures >= task.max_retries:
            changes.update(status="delivery_failed", delivery_error="No se pudo encolar la entrega del resultado a Bolsa.")
        try:
            update = store.update_owned if owned else store.update
            update(job_id, **changes)
        except LockOwnershipLost as lock_exc:
            raise task.retry(exc=lock_exc, countdown=30, max_retries=max(10, task.request.retries + 1)) from lock_exc
        if failures >= task.max_retries:
            raise
        # Quota scheduling may already have incremented Celery's request count
        # many times. The independent persisted counter bounds enqueue failures.
        raise task.retry(
            exc=exc, countdown=min(60, 2 ** (failures - 1)),
            max_retries=max(task.max_retries, task.request.retries + 1),
        ) from exc


@celery_app.task(bind=True, max_retries=5, name="app.tasks.run_extraction")
def run_extraction_task(self: Any, job_id: str) -> dict[str, Any]:
    store = ExtractionJobStore()
    job = store.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "missing"}
    if job["status"] in {"completed", "failed", "delivery_failed"}:
        # Reintenta publicar la entrega si el worker terminó tras guardar el
        # resultado pero antes de que Celery aceptara la tarea de callback.
        _queue_result_delivery(self, store, job_id)
        return {"job_id": job_id, "status": job["status"]}
    if job["status"] in {"delivering", "delivered"}:
        return {"job_id": job_id, "status": job["status"]}
    is_candidate_cv = job.get("payload", {}).get("kind") == "candidate_cv"
    serial_lock_acquired = False
    job_lock_acquired = False
    if is_candidate_cv:
        if not store.acquire_lock(CV_SERIAL_LOCK_ID):
            # Keep this job accepted while another worker owns the single CV
            # slot. This retry is queue contention, not a provider attempt.
            raise self.retry(
                exc=RuntimeError("La cola de CV sigue ocupada."),
                countdown=5,
                max_retries=max(1000, int(self.request.retries) + 1),
            )
        serial_lock_acquired = True
    try:
        if not store.acquire_lock(job_id):
            # A redelivery can race with an execution that still owns its
            # per-job lease. Release the serial slot in finally before retrying.
            countdown = min(600, 30 * (2 ** max(0, int(self.request.retries))))
            retry_options = {"max_retries": max(1000, int(self.request.retries) + 1)} if is_candidate_cv else {}
            raise self.retry(
                exc=RuntimeError("El trabajo sigue bloqueado por otra ejecución."),
                countdown=countdown,
                **retry_options,
            )
        job_lock_acquired = True
        # Refresh after acquiring the lease. Another worker may have completed
        # and released it after our first read, while this task waited in queue.
        job = store.get(job_id)
        if not job:
            return {"job_id": job_id, "status": "missing"}
        if job["status"] in {"completed", "failed", "delivery_failed"}:
            _queue_result_delivery(self, store, job_id, owned=True)
            return {"job_id": job_id, "status": job["status"]}
        if job["status"] in {"delivering", "delivered"}:
            return {"job_id": job_id, "status": job["status"]}
        payload = job["payload"]
        packet_checkpoint = job.get("extraction_packets") if payload["kind"] == "candidate_cv" else None
        completed_packets_at_start = count_completed_packets(packet_checkpoint)
        rate_limit_stalls = int(job.get("rate_limit_stalls", 0))
        provider_failures = int(job.get("provider_failures", 0))
        store.update_owned(job_id, status="processing", error=None)
        if payload["kind"] == "candidate_cv":
            if payload.get("artifact_id"):
                artifact_id = payload["artifact_id"]
                document_format = payload.get("format", "")
                artifact = artifact_path(artifact_id, document_format)
                try:
                    data = artifact.read_bytes()
                except OSError as exc:
                    raise DocumentRejected("El archivo temporal del CV ya no está disponible. Volvé a cargarlo.") from exc
                if hashlib.sha256(data).hexdigest() != payload.get("source_hash"):
                    raise DocumentRejected("El archivo temporal del CV no coincide con la carga original.")
            else:
                # Compatibilidad temporal con trabajos PDF encolados antes del cambio.
                document_format = "pdf"
                artifact_id = uuid4().hex
                artifact = artifact_path(artifact_id, document_format)
                artifact.write_bytes(base64.b64decode(payload["cv_pdf_base64"], validate=True))
            try:
                text = extract_in_subprocess(artifact, document_format)
            finally:
                if not payload.get("artifact_id"):
                    artifact.unlink(missing_ok=True)
            def extraction_heartbeat() -> None:
                job_lock_ok = store.renew_lock(job_id)
                serial_lock_ok = store.renew_lock(CV_SERIAL_LOCK_ID)
                if not job_lock_ok or not serial_lock_ok:
                    raise LockOwnershipLost("Se perdió el lease durante la extracción del CV.")

            provider = settings.LLM_PROVIDER.strip().lower()
            configured_model = (
                settings.OPENAI_MODEL if provider == "openai"
                else settings.GEMINI_MODEL if provider == "gemini"
                else settings.CV_MODEL
            )
            packet_identity = make_packet_identity(
                job_id=job_id,
                analysis_id=int(payload["analysis_id"]),
                candidate_id=int(payload["candidate_id"]),
                cv_version_id=payload.get("cv_version_id"),
                source_hash=payload["source_hash"],
                text=text,
                prompt_version=CV_EXTRACTION_PROMPT_VERSION,
                provider=provider,
                model=configured_model,
                packet_chars=settings.CV_PACKET_CHARACTERS,
                block_chars=settings.CV_BLOQUE_CARACTERES,
                budget=settings.CV_EXTRACTION_MAX_TOKENS,
            )

            def save_packet_checkpoint(value: dict[str, Any]) -> None:
                store.update_owned(job_id, extraction_packets=value)

            result = asyncio.run(process_cv_text(
                text,
                int(payload["candidate_id"]),
                identity=packet_identity,
                checkpoint=packet_checkpoint,
                on_checkpoint=save_packet_checkpoint,
                on_heartbeat=extraction_heartbeat,
            ))
        elif payload["kind"] == "job_offer":
            result = asyncio.run(process_offer_snapshot(payload["offer_snapshot"], int(payload["offer_id"])))
        elif payload["kind"] == "profile_normalization":
            result = asyncio.run(normalize_candidate_profile(payload["profile_snapshot"], int(payload["candidate_id"])))
        else:
            raise ValueError("Unsupported extraction kind.")
        provider_model = getattr(result, "provider_model", None) or (
            settings.OPENAI_MODEL if settings.LLM_PROVIDER.strip().lower() == "openai"
            else settings.GEMINI_MODEL if settings.LLM_PROVIDER.strip().lower() == "gemini"
            else settings.CV_MODEL
        )
        store.update_owned(job_id, status="completed", result=result, error=None, provider_model=provider_model)
        if payload.get("artifact_id"):
            delete_artifact(payload["artifact_id"], payload.get("format", ""))
        _queue_result_delivery(self, store, job_id, owned=True)
        return {"job_id": job_id, "status": "completed"}
    except Retry:
        raise
    except LockOwnershipLost as exc:
        # No se publica un resultado de un worker que ya perdió el lease.
        raise self.retry(
            exc=exc,
            countdown=30,
            max_retries=max(10, self.request.retries + 1),
        ) from exc
    except GeminiUnavailable as exc:
        latest = store.get(job_id) or job
        completed_now = count_completed_packets(latest.get('extraction_packets'))
        # A later packet or consolidation gets its own outage budget after progress.
        previous = 0 if completed_now > completed_packets_at_start else int(job.get('provider_unavailable_failures', 0))
        failures = previous + 1
        exhausted = failures >= self.max_retries
        error = 'Gemini sigue sin estar disponible. Intentá realizar el análisis más tarde.' if exhausted else None
        try:
            store.update_owned(job_id, status='failed' if exhausted else 'processing',
                               error=error, provider_unavailable_failures=failures)
        except LockOwnershipLost as lock_exc:
            raise self.retry(exc=lock_exc, countdown=30, max_retries=max(1000, self.request.retries + 1)) from lock_exc
        if exhausted:
            if payload.get('artifact_id'):
                delete_artifact(payload['artifact_id'], payload.get('format', ''))
            _queue_result_delivery(self, store, job_id, owned=True)
            return {'job_id': job_id, 'status': 'failed'}
        countdown = exc.retry_countdown(failures)
        logger.warning('Extraction deferred for provider outage job=%s failures=%d seconds=%d', job_id, failures, countdown)
        raise self.retry(exc=exc, countdown=countdown, max_retries=max(1000, self.request.retries + 1)) from exc
    except RateLimitExceeded as exc:
        if exc.is_local_wait:
            try:
                store.update_owned(job_id, status="rate_limited", error=None)
            except LockOwnershipLost as lock_exc:
                raise self.retry(exc=lock_exc, countdown=30, max_retries=max(1000, self.request.retries + 1)) from lock_exc
            raise self.retry(
                exc=exc, countdown=max(1, int(exc.retry_after_seconds) + 1),
                max_retries=max(1000, self.request.retries + 1),
            ) from exc
        is_packet_cv = bool(payload and payload.get("kind") == "candidate_cv")
        latest = store.get(job_id) if is_packet_cv else None
        completed_now = count_completed_packets(latest.get("extraction_packets") if latest else None)
        if is_packet_cv:
            rate_limit_stalls = 0 if completed_now > completed_packets_at_start else rate_limit_stalls + 1
        exhausted_no_progress = is_packet_cv and rate_limit_stalls >= 5
        if not is_packet_cv:
            provider_failures += 1
        exhausted_legacy = not is_packet_cv and provider_failures >= self.max_retries
        if exhausted_no_progress or exhausted_legacy:
            error = "Se agotó el límite de solicitudes del proveedor. Intentá analizar el CV más tarde."
            try:
                store.update_owned(job_id, status="failed", error=error, rate_limit_stalls=rate_limit_stalls, provider_failures=provider_failures)
            except LockOwnershipLost as lock_exc:
                raise self.retry(exc=lock_exc, countdown=30, max_retries=max(10, self.request.retries + 1)) from lock_exc
            if payload.get("artifact_id"):
                delete_artifact(payload["artifact_id"], payload.get("format", ""))
            _queue_result_delivery(self, store, job_id, owned=True)
            return {"job_id": job_id, "status": "failed"}
        try:
            store.update_owned(job_id, status="rate_limited", error=None, rate_limit_stalls=rate_limit_stalls, provider_failures=provider_failures)
        except LockOwnershipLost as lock_exc:
            raise self.retry(exc=lock_exc, countdown=30, max_retries=max(10, self.request.retries + 1)) from lock_exc
        if is_packet_cv:
            raise self.retry(
                exc=exc, countdown=max(1, int(exc.retry_after_seconds) + 1),
                max_retries=max(1000, self.request.retries + 1),
            ) from exc
        raise self.retry(exc=exc, countdown=max(1, int(exc.retry_after_seconds) + 1), max_retries=max(1000, self.request.retries + 1)) from exc
    except Exception as exc:
        latest_state = store.get(job_id)
        if latest_state and latest_state.get("status") in {"completed", "failed", "delivering", "delivered", "delivery_failed"}:
            # A callback enqueue failure must not overwrite a result already
            # committed as terminal, even when Celery retries are exhausted.
            logger.error("Could not queue delivery for terminal extraction job %s (%s)", job_id, type(exc).__name__)
            raise
        logger.error("Extraction failed for job %s (%s)", job_id, type(exc).__name__)
        permanent_cv_truncation = (
            payload.get("kind") == "candidate_cv" and isinstance(exc, ProviderResponseTruncated)
        )
        if isinstance(exc, (DocumentRejected, ProviderOutputLimitExceeded, PacketsIdentityError)) or permanent_cv_truncation:
            public_error = (
                "La respuesta del análisis del CV quedó incompleta por el límite de salida. "
                "Volvé a intentarlo más tarde."
                if permanent_cv_truncation
                else str(exc)
            )
            try:
                store.update_owned(job_id, status="failed", error=public_error)
            except LockOwnershipLost as lock_exc:
                raise self.retry(exc=lock_exc, countdown=30, max_retries=max(10, self.request.retries + 1)) from lock_exc
            if payload.get("artifact_id"):
                delete_artifact(payload["artifact_id"], payload.get("format", ""))
            _queue_result_delivery(self, store, job_id, owned=True)
            return {"job_id": job_id, "status": "failed"}
        provider_failures += 1
        if provider_failures < self.max_retries:
            store.update_owned(job_id, provider_failures=provider_failures)
            raise self.retry(exc=exc, countdown=min(60, 2 ** provider_failures), max_retries=max(1000, self.request.retries + 1)) from exc
        try:
            store.update_owned(job_id, status="failed", provider_failures=provider_failures, error="No se pudo procesar el archivo. Revisá el CV y volvé a intentarlo.")
        except LockOwnershipLost as lock_exc:
            raise self.retry(exc=lock_exc, countdown=30, max_retries=max(10, self.request.retries + 1)) from lock_exc
        if payload.get("artifact_id"):
            delete_artifact(payload["artifact_id"], payload.get("format", ""))
        _queue_result_delivery(self, store, job_id, owned=True)
        raise
    finally:
        try:
            if job_lock_acquired:
                store.release_lock(job_id)
        finally:
            if serial_lock_acquired:
                store.release_lock(CV_SERIAL_LOCK_ID)


@celery_app.task(bind=True, max_retries=8, priority=0, name="app.tasks.deliver_extraction_result")
def deliver_extraction_result_task(self: Any, job_id: str) -> dict[str, Any]:
    store = ExtractionJobStore()
    job = store.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "missing"}
    if job["status"] == "delivered":
        return {"job_id": job_id, "status": "delivered"}
    if job["status"] not in {"completed", "failed", "delivering", "delivery_failed"}:
        # A stale or premature delivery must never consume an active extraction.
        return {"job_id": job_id, "status": job["status"]}
    try:
        store.update(job_id, status="delivering")
        notify_extraction_result(_callback_payload(job))
        payload = job['payload']
        if payload.get('user_id') is not None and payload['kind'] == 'candidate_cv':
            publish_cv_analysis_complete(
                payload['user_id'], payload['analysis_id'],
                'completado' if job.get('result') is not None else 'error',
            )
        store.update(job_id, status="delivered", delivery_error=None)
        return {"job_id": job_id, "status": "delivered"}
    except Exception as exc:
        logger.error("Could not deliver extraction result for job %s (%s)", job_id, type(exc).__name__)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=min(300, 2 ** self.request.retries)) from exc
        store.update(job_id, status="delivery_failed", delivery_error="No se pudo entregar el resultado a Bolsa.")
        raise
